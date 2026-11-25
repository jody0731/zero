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
from threading import Lock
from types import SimpleNamespace

import openai
from openai import Stream
from openai.types.responses import Response, ResponseStreamEvent

from core import entries
from core.llm import (
    CutShort,
    ModelHTTPError,
    NoReply,
    StreamFailure,
    UnsupportedModelRequest,
    complete,
    tool_timeline,
)
from core.replay import tool_output
from components.model.apis.chat_completions import list_models, make_client
from components.model.apis.conversation import (
    project_entries, provider_items, reasoning,
)
from components.model.apis.multimodal import (
    TokenEstimate,
    normalize_media_type,
    plan_delivery,
    prepare_with_native_count,
    read_base64,
    request_hash,
    resolve_blob_path,
)
from components.model.apis.search import (
    SearchError,
    SearchResponse,
    Source,
    client,
    http_error,
    is_http_url,
)
from components.model.usage import extract_detail_modality_tokens

# Its name in a server's entry (the line `api`), and in words
NAME = "responses"
TITLE = "the Responses API"

# What ZERO may ask of a model through this API's standard fields
# (features.py): thinking off and on (reasoning.effort), strict JSON
# (text.format json_schema) and JSON mode (text.format json_object)
FEATURES = ("thinking_off", "thinking_on", "strict_json", "json_mode")
# The API refuses a smaller max_output_tokens
MIN_OUTPUT_TOKENS = 16
RETRY_V1_PREFIX = True
# ZERO's thinking efforts as the API's reasoning.effort
EFFORTS = {"off": "none", "low": "low", "high": "high", "max": "max"}
# Why a response is incomplete, in ZERO's words (core/llm.py, Draft); any
# other reason is a reply that didn't complete
INCOMPLETE = {"max_output_tokens": "length", "content_filter": "content_filter"}
# The provider's hosted tool type, part of the Responses wire format.
WEB_SEARCH_TYPE = "web_search"
CALL_ITEM_FIELDS = {
    "type", "id", "call_id", "name", "arguments", "status", "obfuscation", "p",
}
CALL_EVENT_FIELDS = {
    "type", "sequence_number", "item_id", "output_index", "delta", "arguments",
    "name", "obfuscation", "p",
}
# Provider-executed items whose SDK schema carries an explicit status field.
HOSTED_STATUS_KINDS = frozenset({
    "web_search_call",
    "code_interpreter_call",
    "image_generation_call",
    "file_search_call",
    "mcp_call",
})
# Provider-executed items whose schema may omit status on completion.
HOSTED_OPTIONAL_STATUS_KINDS = frozenset({
    "mcp_list_tools",
    "url_context_call",
    "url_context_result",
    "code_execution_call",
    "code_execution_result",
})
# Items carrying an explicit `execution` discriminator ("server" vs "client").
EXECUTION_SCOPED_KINDS = frozenset({
    "tool_search_call",
    "tool_search_output",
})
HOSTED_IN_PROGRESS_STATUSES = frozenset({
    "in_progress", "searching", "interpreting", "generating", "calling",
})
HOSTED_PROGRESS_PREFIXES = (
    "response.web_search_call.",
    "response.code_interpreter_call.",
    "response.code_interpreter_call_code.",
    "response.image_generation_call.",
    "response.file_search_call.",
    "response.mcp_call.",
    "response.mcp_call_arguments.",
    "response.mcp_list_tools.",
)
HOSTED_OUTPUT_FIELDS = (
    "action", "queries", "results", "outputs", "output", "server_label",
    "name", "arguments", "tools", "code", "revised_prompt",
)


def classify_output_item(item):
    """Classify a Responses output item by execution ownership and kind."""
    kind = _item_field(item, "type")
    if kind in ("reasoning", "function_call", "message"):
        return "core"
    status = _item_field(item, "status")
    if kind in HOSTED_STATUS_KINDS:
        if status == "completed":
            return "hosted_completed"
        if status is None or status in HOSTED_IN_PROGRESS_STATUSES:
            return "hosted_pending"
        return "unsupported"
    if kind in HOSTED_OPTIONAL_STATUS_KINDS:
        if _item_field(item, "error"):
            return "unsupported"
        if status in (None, "completed"):
            return "hosted_completed"
        if status in HOSTED_IN_PROGRESS_STATUSES:
            return "hosted_pending"
        return "unsupported"
    if kind in EXECUTION_SCOPED_KINDS:
        if _item_field(item, "execution") != "server":
            return "unsupported"
        if status == "completed":
            return "hosted_completed"
        if status is None or status in HOSTED_IN_PROGRESS_STATUSES:
            return "hosted_pending"
        return "unsupported"
    return "unsupported"


def is_completed_hosted_item(item):
    return classify_output_item(item) == "hosted_completed"


def _hosted_summary(kind, data):
    if kind == "web_search_call":
        action = data.get("action")
        if isinstance(action, Mapping):
            query = action.get("query") or ", ".join(
                str(q) for q in (action.get("queries") or ())
            )
            if query:
                return f"[Hosted web_search_call: {query}]"
    elif kind == "file_search_call":
        queries = data.get("queries")
        if isinstance(queries, list) and queries:
            return f"[Hosted file_search_call: {', '.join(map(str, queries))}]"
    elif kind == "mcp_call":
        server = data.get("server_label")
        name = data.get("name")
        label = f"{server}.{name}" if server and name else (name or server)
        if label:
            return f"[Hosted mcp_call: {label}]"
    elif kind == "mcp_list_tools":
        if server := data.get("server_label"):
            return f"[Hosted mcp_list_tools: {server}]"
    return f"[Hosted {kind}: completed]"


def _hosted_output_fields(data):
    kind = data["type"]
    fields = {
        "kind": kind,
        "execution": "server",
        "status": "completed",
        "summary": _hosted_summary(kind, data),
    }
    if isinstance(item_id := data.get("id"), str) and item_id:
        fields["item_id"] = item_id
    if isinstance(call_id := data.get("call_id"), str) and call_id:
        fields["call_id"] = call_id
    output = {
        key: data[key] for key in HOSTED_OUTPUT_FIELDS
        if key in data and data[key] is not None
    }
    if output:
        fields["output"] = output
    return fields


def _clear_previous_slot(draft, item_id, index):
    if not isinstance(item_id, str) or not item_id:
        return
    seen = draft.stream_state.setdefault("hosted_ids", {})
    prev = seen.get(item_id)
    if prev is not None and prev != index:
        draft.hosted_outputs.pop(prev, None)
        draft.unrecognized_outputs.pop(prev, None)
        stored = draft.continuation_items.get("response_item")
        if stored and stored.get(prev, {}).get("item", {}).get("id") == item_id:
            stored.pop(prev, None)
    seen[item_id] = index


def _hosted_image_descriptor(entry, item, attachment_id=None):
    """Locate the stored `attachments/file` descriptor for a hosted image item."""
    item_id = item.get("id")
    by_att_id = None
    by_item_id = None
    for hosted in entries.matching(entry, "hosted_output", include_private=False):
        if hosted.get("kind") != "image_generation_call":
            continue
        hosted_atts = [
            SimpleNamespace(
                attachment_id=node.get("attachment_id"),
                sha256=node.get("sha256"),
                path=node.get("path"),
            )
            for node in entries.matching(hosted, "attachment", include_private=False)
        ]
        if not hosted_atts:
            continue
        if attachment_id:
            for desc in hosted_atts:
                if desc.attachment_id == attachment_id:
                    by_att_id = desc
                    break
        if by_att_id is not None:
            break
        if item_id and hosted.get("item_id") == item_id and by_item_id is None:
            by_item_id = hosted_atts[0]
    return by_att_id or by_item_id


def _rehydrate_hosted_item(entry, node, thawed_item):
    """Reconstruct `image_generation_call.result` from its stored blob on demand."""
    if thawed_item.get("type") != "image_generation_call":
        return thawed_item
    if isinstance(thawed_item.get("result"), str) and thawed_item["result"]:
        return thawed_item
    desc = _hosted_image_descriptor(
        entry, thawed_item, attachment_id=node.get("attachment_id"),
    )
    if desc is None:
        return None
    blob_path = resolve_blob_path(desc)
    if blob_path is None:
        return None
    try:
        encoded = read_base64(blob_path, expected_sha256=desc.sha256)
    except (OSError, ValueError):
        return None
    return {**thawed_item, "result": encoded}


def response_items(entry, route, content, calls):
    """Provider items still matching the public reply and its calls."""
    stored_nodes = [
        (node, item)
        for node in provider_items(entry, route, "response_item")
        if isinstance(item := node.get("item"), Mapping) and "type" in item
    ]
    stored = [item for _, item in stored_nodes]
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
    replayed = []
    for node, item in stored_nodes:
        if item["type"] in ("reasoning", "function_call") or (
            item["type"] == "message" and keep_text
        ):
            replayed.append(entries.thaw(item))
        elif is_completed_hosted_item(item):
            hydrated = _rehydrate_hosted_item(entry, node, entries.thaw(item))
            if hydrated is not None:
                replayed.append(hydrated)
    return replayed


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


_INPUT_TOKENS_LOCK = Lock()
_INPUT_TOKENS_CACHE = {}
_NO_INPUT_TOKENS_ROUTES = set()
_INPUT_TOKENS_CACHE_MAX = 512
_COUNT_REQUEST_FIELDS = (
    "model", "input", "instructions", "tools", "tool_choice", "reasoning", "text",
)


def clear_input_tokens_cache():
    """Clear cached /responses/input_tokens counts and 404 memoization."""
    with _INPUT_TOKENS_LOCK:
        _INPUT_TOKENS_CACHE.clear()
        _NO_INPUT_TOKENS_ROUTES.clear()


def _count_route_key(client, route):
    if route:
        return str(route)
    base = getattr(client, "base_url", None)
    return str(base) if base is not None else str(id(client))


def count_input_tokens(client, body, *, route=None):
    """Count exact input tokens via POST /v1/responses/input_tokens.

    Cached by `(history_route, plan_hash)`. Memoizes 404/405 responses on
    compatible servers so unsupported endpoints are not probed repeatedly.
    """
    count_body = {
        key: body[key] for key in _COUNT_REQUEST_FIELDS if key in body
    }
    route_key = _count_route_key(client, route)
    digest = request_hash(count_body)
    cache_key = (route_key, digest)
    with _INPUT_TOKENS_LOCK:
        if route_key in _NO_INPUT_TOKENS_ROUTES:
            return None
        cached = _INPUT_TOKENS_CACHE.get(cache_key)
        if cached is not None:
            return cached
    try:
        raw = client.post(
            "/responses/input_tokens", body=count_body, cast_to=object,
        )
    except openai.APIStatusError as error:
        if error.status_code in (404, 405):
            with _INPUT_TOKENS_LOCK:
                _NO_INPUT_TOKENS_ROUTES.add(route_key)
        return None
    except ModelHTTPError as error:
        if error.status_code in (404, 405):
            with _INPUT_TOKENS_LOCK:
                _NO_INPUT_TOKENS_ROUTES.add(route_key)
        return None
    except (openai.APIError, OSError, ValueError):
        return None
    if isinstance(raw, Mapping):
        value = raw.get("input_tokens")
    else:
        value = getattr(raw, "input_tokens", None)
    if type(value) is not int or value < 0:
        return None
    estimate = TokenEstimate(
        tokens=value,
        method="native_input_tokens",
        uncertainty="exact (provider input_tokens)",
        native=True,
        margin_tokens=0,
    )
    with _INPUT_TOKENS_LOCK:
        if len(_INPUT_TOKENS_CACHE) >= _INPUT_TOKENS_CACHE_MAX:
            _INPUT_TOKENS_CACHE.clear()
        _INPUT_TOKENS_CACHE[cache_key] = estimate
    return estimate


def _user_wire_items(text, decisions):
    """Build Responses message items with native images/PDFs or text references."""
    if not decisions:
        if text:
            yield {"type": "message", "role": "user", "content": text}
        return
    parts = []
    has_native_part = False
    if text:
        parts.append({"type": "input_text", "text": text})
    for decision in decisions:
        if decision.is_native:
            encoded = read_base64(
                decision.blob_path, expected_sha256=decision.descriptor.sha256,
            )
            mime = normalize_media_type(decision.descriptor.media_type)
            if decision.modality == "image":
                parts.append({
                    "type": "input_image",
                    "image_url": f"data:{mime};base64,{encoded}",
                    "detail": decision.detail,
                })
                has_native_part = True
            elif decision.modality == "pdf":
                parts.append({
                    "type": "input_file",
                    "filename": decision.descriptor.name,
                    "file_data": f"data:{mime};base64,{encoded}",
                })
                has_native_part = True
            else:
                parts.append({"type": "input_text", "text": decision.reference_text()})
        else:
            parts.append({"type": "input_text", "text": decision.reference_text()})
    if has_native_part:
        yield {"type": "message", "role": "user", "content": parts}
    elif parts:
        joined = "\n\n".join(part["text"] for part in parts if part.get("text"))
        if joined:
            yield {"type": "message", "role": "user", "content": joined}


def _tool_output_wire(result, decisions):
    """Build Responses function_call_output.output string or typed item list."""
    if not decisions:
        return tool_output(result)
    base = tool_output(result, include_attachments=False)
    if any(decision.is_native for decision in decisions):
        parts = []
        if base:
            parts.append({"type": "input_text", "text": base})
        for decision in decisions:
            if decision.is_native:
                encoded = read_base64(
                    decision.blob_path, expected_sha256=decision.descriptor.sha256,
                )
                mime = normalize_media_type(decision.descriptor.media_type)
                if decision.modality == "image":
                    parts.append({
                        "type": "input_image",
                        "image_url": f"data:{mime};base64,{encoded}",
                        "detail": decision.detail,
                    })
                elif decision.modality == "pdf":
                    parts.append({
                        "type": "input_file",
                        "filename": decision.descriptor.name,
                        "file_data": f"data:{mime};base64,{encoded}",
                    })
                else:
                    parts.append({
                        "type": "input_text", "text": decision.reference_text(),
                    })
            else:
                parts.append({"type": "input_text", "text": decision.reference_text()})
        return parts
    refs = [decision.reference_text() for decision in decisions]
    return "\n\n".join(filter(None, (base, *refs)))


def _build_items(conversation, projections, plan, *, route=None,
                 encrypted_reasoning=False):
    instructions = None
    found = []
    timeline = tool_timeline(conversation, lambda node: projections[id(node)].tools)
    for index, (entry, calls, results, _) in enumerate(timeline):
        projected = projections[id(entry)]
        passages = projected.passages
        if (index == 0 and entry.origin == "system"
                and entries.has(entry, "system_prompt")):
            instructions = entries.project(entry)
            if instructions:
                passages = passages[1:]
        entry_decisions = list(plan.for_entry(entry))
        if projected.assistant or calls:
            emitted = list(assistant_items(entry, route, encrypted_reasoning, calls,
                                           projected.content))
            if any(is_completed_hosted_item(item) for item in emitted):
                hosted_texts = {
                    entries.project(node)
                    for node in entries.matching(
                        entry, "hosted_output", include_private=False,
                    )
                }
                passages = tuple(
                    (role, text) for role, text in passages
                    if not (role == "assistant" and text in hosted_texts)
                )
            assistant_passages = [
                {"type": "message", "role": "assistant", "content": text}
                for role, text in passages if role == "assistant"
            ]
            if entry_decisions:
                refs = "\n\n".join(d.reference_text() for d in entry_decisions)
                assistant_passages.append(
                    {"type": "message", "role": "assistant", "content": refs},
                )
                entry_decisions = []
            passages = tuple(
                (role, text) for role, text in passages if role != "assistant"
            )
            # Emit cross-route assistant passages inside the assistant turn
            # before function_call items and function_call_output results.
            call_idx = next(
                (i for i, item in enumerate(emitted)
                 if item.get("type") == "function_call"),
                len(emitted),
            )
            found.extend(emitted[:call_idx])
            found.extend(assistant_passages)
            found.extend(emitted[call_idx:])
        for result, call_id in results:
            res_decisions = plan.for_tool(result)
            found.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": _tool_output_wire(result, res_decisions),
            })
        attached = False
        for role, content in passages:
            if not attached and entry_decisions and role == "user":
                found.extend(_user_wire_items(content, entry_decisions))
                attached = True
            else:
                found.append({"type": "message", "role": role, "content": content})
        if entry_decisions and not attached:
            found.extend(_user_wire_items("", entry_decisions))
    return instructions, found


def items(conversation, *, route=None, encrypted_reasoning=False, project=None,
          capabilities=None, media_budget=None, plan=None):
    """Instructions and input items from the timeline."""
    projections = (project or project_entries)(conversation)
    if plan is None:
        plan = plan_delivery(
            conversation,
            projections,
            api_name=NAME,
            capabilities=capabilities,
            media_budget=media_budget,
        )
    return _build_items(
        conversation, projections, plan, route=route,
        encrypted_reasoning=encrypted_reasoning,
    )


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


def _request_fields(model, instructions, found, *, tools=None, tool_choice=None,
                    effort=None, json_schema=None, json_mode=False,
                    temperature=None, max_output_tokens=None,
                    encrypted_reasoning=False, reasoning_summary=False):
    fields = {"model": model, "input": found, "stream": True, "store": False}
    if instructions is not None:
        fields["instructions"] = instructions
    if tools:
        fields["tools"] = [tool(schema) for schema in tools]
        if tool_choice is not None:
            fields["tool_choice"] = tool_choice
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


def prepare_request(model, messages, *, tools=None, tool_choice=None, effort=None,
                    json_schema=None, json_mode=False, temperature=None,
                    max_output_tokens=None, encrypted_reasoning=False,
                    reasoning_summary=False, route=None, project=None,
                    capabilities=None, media_budget=None, total_budget=None,
                    client=None):
    """Prepare the exact route-specific Responses body and accounting plan."""
    projections = (project or project_entries)(messages)

    def build_body(plan):
        instructions, found = _build_items(
            messages, projections, plan, route=route,
            encrypted_reasoning=encrypted_reasoning,
        )
        return _request_fields(
            model, instructions, found,
            tools=tools, tool_choice=tool_choice, effort=effort,
            json_schema=json_schema, json_mode=json_mode,
            temperature=temperature, max_output_tokens=max_output_tokens,
            encrypted_reasoning=encrypted_reasoning,
            reasoning_summary=reasoning_summary,
        )

    native_counter = (
        (lambda body, *, route=None: count_input_tokens(client, body, route=route))
        if client is not None else None
    )
    return prepare_with_native_count(
        messages,
        projections,
        api_name=NAME,
        capabilities=capabilities,
        media_budget=media_budget,
        total_budget=total_budget,
        route=route,
        build_body=build_body,
        native_counter=native_counter,
    )


def request(model, messages, *, tools=None, tool_choice=None, effort=None,
            json_schema=None, json_mode=False, temperature=None,
            max_output_tokens=None, encrypted_reasoning=False,
            reasoning_summary=False, route=None, project=None,
            capabilities=None, media_budget=None, total_budget=None,
            client=None):
    """The request's fields. `json_schema`: {"name", "schema"}, strict JSON;
    `json_mode`: any JSON object."""
    return prepare_request(
        model, messages,
        tools=tools, tool_choice=tool_choice, effort=effort,
        json_schema=json_schema, json_mode=json_mode,
        temperature=temperature, max_output_tokens=max_output_tokens,
        encrypted_reasoning=encrypted_reasoning,
        reasoning_summary=reasoning_summary,
        route=route, project=project, capabilities=capabilities,
        media_budget=media_budget, total_budget=total_budget,
        client=client,
    ).body


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
    prepared_body = standard.pop("prepared_body", None)
    if prepared_body is not None:
        return stream_response(client, prepared_body)
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
    by_modality = extract_detail_modality_tokens(used, "input_tokens_details")
    if by_modality:
        draft.input_tokens_by_modality = by_modality


def _is_benign_call_default(key, value):
    if key == "caller":
        return value is None or (
            isinstance(value, Mapping) and dict(value) == {"type": "direct"}
        )
    if key == "namespace":
        return value is None
    if key in ("async", "async_"):
        return value is None or value is False
    return False


def remember_call_fields(call, data, known):
    extra = {
        key: value for key, value in data.items()
        if key not in known and not _is_benign_call_default(key, value)
    }
    if extra:
        call.setdefault("wire_fields", {}).update(extra)


def _item_field(item, name, default=None):
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _dump_item(item, *, exclude_unset=False, exclude_none=False):
    if isinstance(item, Mapping):
        if exclude_none:
            return {k: v for k, v in item.items() if v is not None}
        return dict(item)
    return item.model_dump(
        mode="json", by_alias=True,
        exclude_unset=exclude_unset, exclude_none=exclude_none,
    )


def remember_item(draft, index, item, *, done=True, ignored_kinds=()):
    kind = _item_field(item, "type")
    if kind in ignored_kinds:
        return False
    category = classify_output_item(item)
    if category == "hosted_completed":
        data = _dump_item(item, exclude_none=True)
        data.pop("obfuscation", None)
        data.pop("p", None)
        item_id = data.get("id")
        _clear_previous_slot(draft, item_id, index)
        draft.unrecognized_outputs.pop(index, None)
        stored = draft.continuation_items.setdefault("response_item", {})
        previous = stored.get(index, {}).get("item")
        if (isinstance(previous, Mapping) and previous.get("type") == kind
                and (not item_id or previous.get("id") == item_id)):
            data = {**previous, **data}
        draft.hosted_outputs[index] = _hosted_output_fields(data)
        stored[index] = {"item": data}
        return True
    if category == "hosted_pending" and not done:
        _clear_previous_slot(draft, _item_field(item, "id"), index)
        draft.unrecognized_outputs[index] = _dump_item(item)
        return True
    if category != "core":
        draft.hosted_outputs.pop(index, None)
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
    data.pop("obfuscation", None)
    data.pop("p", None)
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
        remember_call_fields(
            call, event.model_dump(mode="json", exclude_unset=True),
            CALL_EVENT_FIELDS,
        )
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
    if isinstance(kind, str) and kind.startswith(HOSTED_PROGRESS_PREFIXES):
        return True
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
        except (CutShort, UnsupportedModelRequest, openai.APIError):
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
