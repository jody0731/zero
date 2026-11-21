"""OpenAI's Responses API (`POST /v1/responses`), the one OpenAI, DeepSeek,
xAI and Codex have moved to.

ZERO keeps no state on the server (`store` false): every request carries the
whole conversation, as typed items. ZERO's messages (the Chat Completions
API's shape, core/llm.py) become them here: the first system message the
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

import openai
from openai import Stream
from openai.types.responses import Response, ResponseStreamEvent

from core.llm import CutShort, NoReply, StreamFailure, complete
from model.apis.search import (
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


def response_items(message):
    """Completed provider items that still match the saved assistant message."""
    stored = (message.get("provider_state") or {}).get("items") or ()
    if not stored:
        return []
    calls = {call["id"]: call["function"] for call in message.get("tool_calls", ())}
    native_calls = {item["call_id"]: {
        "name": item["name"], "arguments": item["arguments"],
    } for item in stored if item["type"] == "function_call"}
    if native_calls and native_calls != calls:
        return []
    native_messages = [item for item in stored if item["type"] == "message"]
    if native_messages and message.get("content"):
        parts = [part for item in native_messages for part in item["content"]]
        if not same_text(parts, message["content"]):
            return []
    found = []
    for item in stored:
        kind = item["type"]
        if kind in ("reasoning", "function_call"):
            found.append(item)
        elif kind == "message" and message.get("content"):
            found.append(item)
    return found


def same_text(parts, content):
    if any(part.get("type") != "output_text" for part in parts):
        return False
    raw = "".join(part["text"] for part in parts)
    if raw == content:
        return True
    try:
        # Reformatting accepted public JSON does not change its content.
        return json.loads(raw) == json.loads(content)
    except ValueError:
        return False


def items(messages, *, encrypted_reasoning=False):
    """(instructions, input items) for ZERO's messages."""
    instructions = None
    found = []
    for index, message in enumerate(messages):
        role = message["role"]
        if role == "system" and index == 0:
            instructions = message["content"]
        elif role in ("system", "user"):
            found.append({"type": "message", "role": role,
                          "content": message["content"]})
        elif role == "assistant":
            replayed = response_items(message)
            found.extend(replayed)
            kinds = {item["type"] for item in replayed}
            if ("reasoning" not in kinds and not encrypted_reasoning
                    and message.get("reasoning_content")):
                found.append({
                    "type": "reasoning", "summary": [],
                    "content": [{"type": "reasoning_text",
                                 "text": message["reasoning_content"]}],
                })
            if "message" not in kinds and message.get("content"):
                found.append({"type": "message", "role": "assistant",
                              "content": message["content"]})
            replayed_calls = {item["call_id"] for item in replayed
                              if item["type"] == "function_call"}
            for call in message.get("tool_calls", ()):
                if call["id"] in replayed_calls:
                    continue
                found.append({
                    "type": "function_call", "call_id": call["id"],
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"],
                })
        elif role == "tool":
            found.append({"type": "function_call_output",
                          "call_id": message["tool_call_id"],
                          "output": message["content"]})
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
            encrypted_reasoning=False):
    """The request's fields. `json_schema`: {"name", "schema"}, strict JSON;
    `json_mode`: any JSON object."""
    instructions, found = items(messages, encrypted_reasoning=encrypted_reasoning)
    fields = {"model": model, "input": found, "stream": True, "store": False}
    if instructions is not None:
        fields["instructions"] = instructions
    if tools:
        fields["tools"] = [tool(schema) for schema in tools]
    if effort is not None:
        fields["reasoning"] = {"effort": EFFORTS.get(effort, effort)}
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


def remember_item(draft, index, item):
    kind = getattr(item, "type", None)
    if kind not in ("reasoning", "function_call", "message"):
        return
    data = item.model_dump(mode="json", exclude_none=True)
    previous = draft.response_items.get(index, {})
    if kind == "reasoning" and previous.get("encrypted_content"):
        data.setdefault("encrypted_content", previous["encrypted_content"])
    draft.response_items[index] = data
    if kind == "function_call":
        call = draft.call(index)
        call.update(id=item.call_id, name=item.name, arguments=item.arguments or "")


def fold(draft, event):
    """One event into the draft; True if it carried anything (the response
    made, a part begun: nothing, so the silence clock runs on). Raises
    CutShort for a response that failed, or that is incomplete for a reason
    other than the output limit or a content filter."""
    kind = getattr(event, "type", None)
    if kind == "response.output_text.delta":
        draft.content += event.delta
        return bool(event.delta)
    if kind in ("response.reasoning_text.delta",
                "response.reasoning_summary_text.delta"):
        draft.reasoning += event.delta
        return bool(event.delta)
    if kind in ("response.output_item.added", "response.output_item.done"):
        item = event.item
        if kind == "response.output_item.done":
            remember_item(draft, event.output_index, item)
            return item.type in ("reasoning", "function_call", "message")
        if getattr(item, "type", None) != "function_call":
            return False
        call = draft.call(event.output_index)
        call["id"] = item.call_id
        call["name"] = item.name
        # Whole when done; while it streams, the deltas build it
        if item.arguments:
            call["arguments"] = item.arguments or ""
        return True
    if kind == "response.function_call_arguments.delta":
        draft.call(event.output_index)["arguments"] += event.delta
        return bool(event.delta)
    if kind in ("response.completed", "response.incomplete"):
        for index, item in enumerate(getattr(event.response, "output", ()) or ()):
            remember_item(draft, index, item)
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
        return fold(draft, event)

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
