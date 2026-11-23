"""OpenAI's Responses API (`POST /v1/responses`), the one OpenAI, DeepSeek,
xAI and Codex have moved to.

ZERO keeps no state on the server (`store` false): every request carries the
whole conversation, as typed items. The first system prompt becomes the
request's `instructions`; a message each turn of text; an assistant turn's
thinking an exact provider item or a legacy plain-text item, its calls
`function_call` items, and each result a `function_call_output` paired by
`call_id`. Its stream is
typed events: deltas of text, reasoning and a call's arguments, each output
item whole when it is done, then the response completed, incomplete (the
output limit, or another reason) or failed, with its usage.
The prepared JSON goes through the SDK's public post method; its transport
and typed event parser remain the SDK's, without a second request-type walk.
"""

import json
from collections.abc import Mapping

import openai
from openai import Stream
from openai.types.responses import Response, ResponseStreamEvent

from core import entries
from core.llm import CutShort, NoReply, StreamFailure, complete, tool_timeline
from core.replay import tool_output
from components.model.apis.conversation import (
    project_entries, provider_items, reasoning,
)
from components.model.apis.search import (
    SearchError,
    SearchResponse,
    Source,
    client,
    http_error,
    is_http_url,
)

# Its name in a server's entry (the line `api`), and in words
NAME = "responses"
TITLE = "the Responses API"

# What ZERO may ask of a model through this API's standard fields
# (features.py): thinking off and on (reasoning.effort), strict JSON
# (text.format json_schema) and JSON mode (text.format json_object)
FEATURES = ("thinking_off", "thinking_on", "strict_json", "json_mode")
# The API refuses a smaller max_output_tokens
MIN_OUTPUT_TOKENS = 16
# ZERO's thinking efforts as the API's reasoning.effort
EFFORTS = {"off": "none", "low": "low", "high": "high", "max": "max"}
# Why a response is incomplete, in ZERO's words (core/llm.py, Draft); any
# other reason is a reply that didn't complete
INCOMPLETE = {"max_output_tokens": "length", "content_filter": "content_filter"}
# The provider's hosted tool type, part of the Responses wire format.
WEB_SEARCH_TYPE = "web_search"
CALL_ITEM_FIELDS = {"type", "id", "call_id", "name", "arguments", "status"}
CALL_EVENT_FIELDS = {
    "type", "sequence_number", "item_id", "output_index", "delta", "arguments", "name",
}


def response_items(entry, route, content, calls):
    """Provider items still matching the public reply and its calls."""
    stored = [item for node in provider_items(entry, route, "response_item")
              if isinstance(item := node.get("item"), Mapping) and "type" in item]
    native_calls = {item["call_id"]: (item["name"], item["arguments"])
                    for item in stored if item["type"] == "function_call"}
    expected = {entries.call_id(call): (call.get("name"), call.get("arguments"))
                for call in calls}
    if native_calls and native_calls != expected:
        return []
    native_messages = [item for item in stored if item["type"] == "message"]
    keep_text = bool(content)
    if native_messages and keep_text:
        parts = [part for item in native_messages for part in item["content"]]
        keep_text = (all(part.get("type") == "output_text" for part in parts)
                     and "".join(part["text"] for part in parts) == content)
    return [entries.thaw(item) for item in stored
            if item["type"] in ("reasoning", "function_call")
            or (item["type"] == "message" and keep_text)]


def assistant_items(entry, route, encrypted_reasoning, calls, content):
    replayed = response_items(entry, route, content, calls)
    yield from replayed
    kinds = {item["type"] for item in replayed}
    thought = reasoning(entry, route)
    if "reasoning" not in kinds and not encrypted_reasoning and thought:
        yield {"type": "reasoning", "summary": [],
               "content": [{"type": "reasoning_text", "text": thought}]}
    if "message" not in kinds and content:
        yield {"type": "message", "role": "assistant", "content": content}
    replayed_calls = {item["call_id"] for item in replayed
                      if item["type"] == "function_call"}
    for call in calls:
        call_id = entries.call_id(call)
        if call_id not in replayed_calls:
            yield {"type": "function_call", "call_id": call_id,
                   "name": call.get("name"), "arguments": call.get("arguments")}


def items(conversation, *, route=None, encrypted_reasoning=False, project=None):
    """Instructions and input items from the timeline."""
    instructions = None
    found = []
    projections = (project or project_entries)(conversation)
    timeline = tool_timeline(conversation, lambda node: projections[id(node)].tools)
    for index, (entry, calls, results, _) in enumerate(timeline):
        projected = projections[id(entry)]
        passages = projected.passages
        if (index == 0 and entry.origin == "system"
                and entries.has(entry, "system_prompt")):
            instructions = entries.project(entry)
            if instructions:
                passages = passages[1:]
        if projected.assistant or calls:
            found.extend(assistant_items(entry, route, encrypted_reasoning, calls,
                                         projected.content))
        for result, call_id in results:
            found.append({"type": "function_call_output", "call_id": call_id,
                          "output": tool_output(result)})
        for role, content in passages:
            found.append({"type": "message", "role": role, "content": content})
    return instructions, found


def tool(schema):
    """A tool's schema (the Chat Completions API's shape, as ZERO's tools
    declare it) as this API's function tool. Not strict: the API's default
    is, and ZERO's schemas leave arguments optional."""
    function = schema["function"]
    return {
        "type": "function", "name": function["name"],
        "description": function.get("description", ""),
        "parameters": function.get("parameters", {}),
        "strict": False,
    }


def request(model, messages, *, tools=None, effort=None, json_schema=None,
            json_mode=False, temperature=None, max_output_tokens=None,
            encrypted_reasoning=False, reasoning_summary=False, route=None,
            project=None):
    """The request's fields. `json_schema`: {"name", "schema"}, strict JSON;
    `json_mode`: any JSON object."""
    instructions, found = items(
        messages, route=route, encrypted_reasoning=encrypted_reasoning, project=project,
    )
    fields = {"model": model, "input": found, "stream": True, "store": False}
    if instructions is not None:
        fields["instructions"] = instructions
    if tools:
        fields["tools"] = [tool(schema) for schema in tools]
    if encrypted_reasoning:
        fields["include"] = ["reasoning.encrypted_content"]
    if effort is not None:
        wire_effort = EFFORTS.get(effort, effort)
        fields["reasoning"] = {
            "effort": wire_effort,
            **({"summary": "auto"}
               if reasoning_summary and wire_effort != "none" else {}),
        }
    if json_schema is not None:
        fields["text"] = {"format": {
            "type": "json_schema", "name": json_schema["name"],
            "strict": True, "schema": json_schema["schema"],
        }}
    elif json_mode:
        fields["text"] = {"format": {"type": "json_object"}}
    if temperature is not None:
        fields["temperature"] = temperature
    if max_output_tokens is not None:
        fields["max_output_tokens"] = max_output_tokens
    return fields


def request_options(server, *, encrypted=False, zero_temperature=False):
    """Adapter request kwargs implied by a server's declared capabilities."""
    options = {}
    if server is not None:
        if encrypted and server.replays_encrypted_reasoning:
            options["encrypted_reasoning"] = True
        if server.requests_reasoning_summary:
            options["reasoning_summary"] = True
    if zero_temperature and (server is None or server.accepts_zero_temperature):
        options["temperature"] = 0
    return options


def keeps_continuation(server):
    """Whether to persist route-scoped continuation items for `server`."""
    return True if server is None else server.replays_response_items


def open_stream(client, model, messages, **standard):
    """Send prepared JSON; the SDK handles HTTP and parses streamed events."""
    return stream_response(client, request(model, messages, **standard))


def stream_response(client, body):
    return client.post(
        "/responses", body=body,
        cast_to=Response, stream=True, stream_cls=Stream[ResponseStreamEvent],
    )


def usage(draft, response):
    used = getattr(response, "usage", None)
    if used is None:
        return
    details = getattr(used, "input_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) or 0
    draft.tokens = (used.input_tokens or 0, cached, used.output_tokens or 0)


def remember_call_fields(call, data, known):
    extra = {key: value for key, value in data.items() if key not in known}
    if extra:
        call.setdefault("wire_fields", {}).update(extra)


def _item_field(item, name, default=None):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _dump_item(item, *, exclude_unset=False, exclude_none=False):
    if isinstance(item, dict):
        if exclude_none:
            return {k: v for k, v in item.items() if v is not None}
        return dict(item)
    return item.model_dump(
        mode="json", exclude_unset=exclude_unset, exclude_none=exclude_none,
    )


def remember_item(draft, index, item, *, done=True, ignored_kinds=()):
    kind = _item_field(item, "type")
    if kind not in ("reasoning", "function_call", "message"):
        if kind not in ignored_kinds:
            draft.unrecognized_outputs[index] = _dump_item(item)
        return False
    if kind == "function_call":
        call = draft.call(index)
        call.update(
            id=_item_field(item, "call_id"),
            name=_item_field(item, "name"),
        )
        arguments = _item_field(item, "arguments")
        if arguments:
            call["arguments"] = arguments
        remember_call_fields(
            call, _dump_item(item, exclude_unset=True), CALL_ITEM_FIELDS,
        )
    if not done:
        if kind == "reasoning":
            encrypted = _item_field(item, "encrypted_content")
            if encrypted:
                draft.stream_state.setdefault("early_encrypted", {})[index] = (
                    encrypted
                )
        return kind == "function_call"
    data = _dump_item(item, exclude_none=True)
    stored = draft.continuation_items.setdefault("response_item", {})
    previous = stored.get(index, {}).get("item", {})
    early = draft.stream_state.get("early_encrypted", {}).get(index)
    if kind == "reasoning":
        inherited = previous.get("encrypted_content") or early
        if inherited:
            data.setdefault("encrypted_content", inherited)
    stored[index] = {"item": data}
    return True


def _fold(draft, event, *, ignored_kinds=()):
    kind = getattr(event, "type", None)
    if kind == "response.output_text.delta":
        draft.content += event.delta
        return bool(event.delta)
    if kind in ("response.reasoning_text.delta",
                "response.reasoning_summary_text.delta"):
        draft.reasoning += event.delta
        return bool(event.delta)
    if kind in ("response.output_item.added", "response.output_item.done"):
        return remember_item(draft, event.output_index, event.item,
                             done=kind == "response.output_item.done",
                             ignored_kinds=ignored_kinds)
    if kind in ("response.function_call_arguments.delta",
                "response.function_call_arguments.done"):
        call = draft.call(event.output_index)
        remember_call_fields(call, event.model_dump(mode="json", exclude_unset=True),
                             CALL_EVENT_FIELDS)
        if kind.endswith(".delta"):
            call["arguments"] += event.delta
            return bool(event.delta)
        call["arguments"] = event.arguments
        return True
    if kind in ("response.completed", "response.incomplete"):
        for index, item in enumerate(getattr(event.response, "output", ()) or ()):
            remember_item(draft, index, item, ignored_kinds=ignored_kinds)
    if kind == "response.completed":
        usage(draft, event.response)
        draft.finish_reason = "tool_calls" if draft.calls else "stop"
        return True
    if kind == "response.incomplete":
        usage(draft, event.response)
        details = getattr(event.response, "incomplete_details", None)
        reason = getattr(details, "reason", None)
        if reason not in INCOMPLETE:
            raise CutShort(f"the response is incomplete ({reason})")
        draft.finish_reason = INCOMPLETE[reason]
        return True
    if kind == "response.failed":
        error = getattr(event.response, "error", None)
        raise StreamFailure(getattr(error, "code", None),
                            getattr(error, "message", None))
    if kind == "error":
        raise StreamFailure(getattr(event, "code", None),
                            getattr(event, "message", None))
    return False


def fold(draft, event):
    """One event into the draft; True if it carried anything (the response
    made, a part begun: nothing, so the silence clock runs on). Raises
    CutShort for a response that failed, or that is incomplete for a reason
    other than the output limit or a content filter."""
    return _fold(draft, event)


class ResponsesSearch:
    """One isolated Responses request with a hosted web search."""

    NAME = "responses_search"
    MAX_RESULTS = 50
    REQUIRES_MODEL = True

    def __init__(self, query, count):
        self.query = query
        self.count = count
        self.response = None

    @staticmethod
    def endpoint(base_url):
        return base_url.rstrip("/") + "/responses"

    def open_stream(self, sdk, model, messages):
        return stream_response(sdk, {
            "model": model,
            "input": json.dumps(
                {"query": self.query, "count": self.count}, ensure_ascii=False,
            ),
            "instructions": (
                "Search the web for the query. Return up to the requested count "
                "of relevant sources."
            ),
            "tools": [{"type": WEB_SEARCH_TYPE, "search_context_size": "low"}],
            "tool_choice": {"type": WEB_SEARCH_TYPE},
            "max_tool_calls": 1,
            "parallel_tool_calls": False,
            "include": ["web_search_call.action.sources"],
            "max_output_tokens": 512,
            "store": False,
            "stream": True,
        })

    def fold(self, draft, event):
        if event.type in ("response.completed", "response.incomplete"):
            self.response = event.response
        if event.type.startswith("response.web_search_call."):
            return True
        return _fold(draft, event, ignored_kinds=("web_search_call",))

    @classmethod
    def search(cls, service, query, count, seconds, transport, check):
        lookup = cls(query, count)
        try:
            with client(seconds, transport) as http:
                sdk = openai.OpenAI(
                    api_key=service.key,
                    base_url=service.url.removesuffix("/responses"),
                    http_client=http, max_retries=0, timeout=seconds,
                )
                check()
                reply = complete(
                    lookup, sdk, service.model, [], silence=seconds, check=check,
                )
        except openai.APIStatusError as error:
            raise http_error(error.status_code) from None
        except (NoReply, openai.APITimeoutError):
            raise SearchError(
                f"The search response gave no data for {seconds:g} s."
            ) from None
        except openai.APIConnectionError:
            raise SearchError("The connection to the search service failed.") \
                from None
        except (CutShort, openai.APIError):
            raise SearchError("The service did not complete its search response.") \
                from None
        return lookup.sources(reply.content)

    def sources(self, summary):
        output = getattr(self.response, "output", ())
        calls = [item for item in output if item.type == "web_search_call"]
        if not any(call.status == "completed" for call in calls):
            raise SearchError("The response did not include a completed search.")
        found = {}
        for item in output:
            if item.type == "message":
                for part in item.content:
                    for citation in getattr(part, "annotations", ()):
                        if citation.type == "url_citation" and is_http_url(
                            citation.url
                        ):
                            found[citation.url] = Source(citation.url, citation.title)
        for call in calls:
            action = getattr(call, "action", None)
            for source in getattr(action, "sources", None) or ():
                if is_http_url(source.url):
                    found.setdefault(source.url, Source(source.url))
        sources = list(found.values())[:self.count]
        return SearchResponse(sources, (summary or "") if sources else "")
