"""OpenAI's Chat Completions API (`POST /v1/chat/completions`), the one
nearly every model server speaks: Ollama, llama-server, vLLM, and the
OpenAI-shaped doors of the big servers.

The adapter projects conversation entries into this API's messages and
sends JSON through the SDK's public post method, without a second
walk through its request types. Its stream is chunks: deltas of text, reasoning and
tool calls, a finish reason, then the usage (asked for with
stream_options). Reasoning isn't in the API's own reference; servers that
show it send `reasoning_content` (llama-server, DeepSeek) or `reasoning`
(Ollama, vLLM, OpenRouter), and both are read.
"""

from collections.abc import Mapping
import re
from threading import Lock
from time import monotonic

import httpx2
from openai import OpenAI, Stream
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from core import entries
from core.llm import CutShort, tool_batches
from core.replay import tool_output
from components.model import MODALITIES, normalize_modalities_list
from components.model.apis.conversation import (
    project_entries, provider_items, reasoning,
)
from components.model.apis.multimodal import (
    audio_format_of,
    normalize_media_type,
    plan_delivery,
    prepare_with_native_count,
    read_base64,
)
from components.model.limits import (
    TokenLimits, endpoint, model_id, smaller, token_count,
)
from components.model.usage import extract_detail_modality_tokens

# Its name in a server's entry (the line `api`), and in words
NAME = "chat_completions"
TITLE = "the Chat Completions API"

# What ZERO may ask of a model through this API's standard fields
# (features.py): thinking off and on (reasoning_effort), strict JSON
# (response_format json_schema) and JSON mode (response_format json_object);
# and one field of llama.cpp's own, prompt progress (return_progress), sent
# only to a model measured to honour it
FEATURES = ("thinking_off", "thinking_on", "strict_json", "json_mode",
            "prefill_progress")
MIN_OUTPUT_TOKENS = 1
RETRY_V1_PREFIX = True
TOTAL_FIELDS = (
    "max_model_len", "context_length", "max_context_length", "context_window",
)
SHOW_SECONDS = 3
SHOW_FRESH_SECONDS = 60
NO_KEY = "none"
_NUM_CTX_RE = re.compile(r"(?m)^\s*num_ctx\s+(\d+)\s*$")
_MISSING_MODEL_RE = re.compile(r"\bmodel\b.*\bnot\s+found\b", re.IGNORECASE)
_show_lock = Lock()
_show_cache = {}
_no_show_endpoints = set()
# ZERO's thinking efforts as the API's reasoning_effort
EFFORTS = {"off": "none", "low": "low", "high": "high", "max": "max"}
# The API's finish reasons, in ZERO's words (core/llm.py, Draft); any other
# is a reply that didn't complete
FINISHED = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "content_filter": "content_filter",
}


def make_client(url, key, timeout):
    """Create an OpenAI SDK client with retries disabled."""
    return OpenAI(base_url=url, api_key=key, timeout=timeout, max_retries=0)


def _modality_map(items, *, file_as_pdf=False):
    """Map an exhaustive list of modality tokens to tri-state bool dict."""
    if not isinstance(items, (list, tuple)):
        return None
    mapped = []
    for item in items:
        if not isinstance(item, str):
            continue
        token = item.strip().lower()
        if file_as_pdf and token == "file":
            token = "pdf"
        mapped.append(token)
    known = set(normalize_modalities_list(mapped))
    if not known:
        return None
    return {name: (name in known) for name in MODALITIES}


def _model_capabilities(data):
    """Extract passive capabilities from OpenRouter-style /v1/models metadata."""
    caps = {}
    arch = data.get("architecture")
    if isinstance(arch, dict):
        in_map = _modality_map(arch.get("input_modalities"), file_as_pdf=True)
        out_map = _modality_map(arch.get("output_modalities"), file_as_pdf=False)
        modality = arch.get("modality")
        if isinstance(modality, str) and "->" in modality:
            left, _, right = modality.partition("->")
            if in_map is None:
                in_map = _modality_map(left.split("+"), file_as_pdf=True)
            if out_map is None:
                out_map = _modality_map(right.split("+"), file_as_pdf=False)
        if in_map is not None:
            caps["input"] = in_map
        if out_map is not None:
            caps["output"] = out_map
    params = data.get("supported_parameters")
    if isinstance(params, list) and params:
        names = {p.strip().lower() for p in params if isinstance(p, str)}
        if names:
            caps["tool_calling"] = "tools" in names
            if "reasoning" in names or "include_reasoning" in names:
                caps["reasoning"] = True
    reasoning_meta = data.get("reasoning")
    if isinstance(reasoning_meta, dict) and isinstance(
        reasoning_meta.get("supported"), bool,
    ):
        caps["reasoning"] = reasoning_meta["supported"]
    return caps


def list_models(url, key, timeout):
    """List OpenAI-compatible models with normalized token limits and metadata."""
    with make_client(url, key, timeout) as client:
        items = client.models.list().data
    found = []
    for item in items:
        if not model_id(item.id):
            continue
        data = item.model_dump()
        window, loads_window = runtime_context_window(data)
        window = token_count(window)
        if not loads_window:
            window = next((count for name in TOTAL_FIELDS
                           if (count := token_count(data.get(name)))), None)
        limits = TokenLimits.read({**data, "context_window": window})
        row = {"id": item.id, "context_window": window, **limits.as_dict()}
        reasoning_meta = data.get("reasoning")
        if isinstance(reasoning_meta, dict):
            row["reasoning"] = reasoning_meta
        if caps := _model_capabilities(data):
            row["capabilities"] = caps
        if loads_window:
            row["context_reported_on_load"] = True
        found.append(row)
    return found


def clear_show_cache():
    """Clear lazy Ollama /api/show caches and negative-404 memoization."""
    with _show_lock:
        _show_cache.clear()
        _no_show_endpoints.clear()


def parse_ollama_show(payload):
    """Extract context window and capabilities from Ollama POST /api/show JSON."""
    if not isinstance(payload, dict):
        return {}
    num_ctx = None
    parameters = payload.get("parameters")
    if isinstance(parameters, str):
        match = _NUM_CTX_RE.search(parameters)
        if match:
            num_ctx = token_count(int(match.group(1)))
    info_ctx = None
    model_info = payload.get("model_info")
    if isinstance(model_info, dict):
        lengths = [
            count for key, value in model_info.items()
            if isinstance(key, str)
            and (key == "context_length" or key.endswith(".context_length"))
            and (count := token_count(value)) is not None
        ]
        if lengths:
            info_ctx = min(lengths)
    window = smaller(num_ctx, info_ctx)
    found = {}
    if window is not None:
        found["context_window"] = window
    raw_caps = payload.get("capabilities")
    if isinstance(raw_caps, list):
        names = {
            item.strip().lower() for item in raw_caps
            if isinstance(item, str) and item.strip()
        }
        if names:
            can_complete = "completion" in names
            caps = {
                "input": {
                    "text": can_complete or "vision" in names,
                    "image": "vision" in names,
                    "audio": "audio" in names,
                    "video": False,
                    "pdf": False,
                },
                "output": {
                    "text": can_complete,
                    "image": False,
                    "audio": False,
                    "video": False,
                    "pdf": False,
                },
                "tool_calling": "tools" in names,
                "reasoning": "thinking" in names,
            }
            found["capabilities"] = caps
            found["reasoning"] = "thinking" in names
    return found


def _is_ollama_missing_model(reply):
    """True when a 404 came from Ollama's /api/show for an unknown model."""
    try:
        data = reply.json()
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    message = data.get("error")
    return isinstance(message, str) and bool(_MISSING_MODEL_RE.search(message))


def show_model(url, model, key=None, timeout=SHOW_SECONDS, *, transport=None,
               cached=True):
    """Lazily query Ollama POST /api/show for one selected model.

    Caches positive results per (endpoint, model) and memoizes 404/405
    responses (endpoint-wide for unsupported routes, per-model for missing
    Ollama models). Never called inside list_models.
    """
    if not model_id(model):
        return None
    address = endpoint(url)
    cache_key = (address, model)
    with _show_lock:
        if cached and address in _no_show_endpoints:
            return None
        if cached and cache_key in _show_cache:
            fetched_at, value = _show_cache[cache_key]
            if monotonic() - fetched_at < SHOW_FRESH_SECONDS:
                return value
    root = address.removesuffix("/v1")
    headers = (
        {"Authorization": f"Bearer {key}"}
        if isinstance(key, str) and key and key != NO_KEY else {}
    )
    try:
        with httpx2.Client(
            timeout=timeout, transport=transport, follow_redirects=False,
        ) as http:
            reply = http.post(
                f"{root}/api/show", json={"model": model}, headers=headers,
            )
    except httpx2.HTTPError:
        return None
    if reply.status_code in (404, 405):
        with _show_lock:
            if reply.status_code == 404 and _is_ollama_missing_model(reply):
                _show_cache[cache_key] = (monotonic(), None)
            else:
                _no_show_endpoints.add(address)
        return None
    if not reply.is_success:
        return None
    try:
        payload = reply.json()
    except ValueError:
        return None
    parsed = parse_ollama_show(payload)
    result = parsed or None
    with _show_lock:
        _show_cache[cache_key] = (monotonic(), result)
    return result


def assistant_message(entry, calls, route, content):
    found = {"role": "assistant"}
    if content:
        found["content"] = content
    details = [entries.thaw(node.get("item")) for node in
               provider_items(entry, route, "reasoning_detail")]
    if details:
        found["reasoning_details"] = details
    elif thought := reasoning(entry, route):
        found["reasoning_content"] = thought
    signatures = {node.get("call_id"): node for node in
                  provider_items(entry, route, "thought_signature")}
    if calls:
        wire = []
        for call in calls:
            call_id = entries.call_id(call)
            function = {"name": call.get("name"), "arguments": call.get("arguments")}
            item = {"id": call_id, "type": "function", "function": function}
            signature = signatures.get(call_id)
            if signature is not None and all(
                signature.get(key) == value for key, value in function.items()
            ):
                if signature.get("extra_content") is not None:
                    item["extra_content"] = entries.thaw(
                        signature.get("extra_content"),
                    )
                if signature.get("thought_signature") is not None:
                    item["thought_signature"] = signature.get("thought_signature")
            wire.append(item)
        found["tool_calls"] = wire
    return found


def _media_part(decision):
    encoded = read_base64(
        decision.blob_path, expected_sha256=decision.descriptor.sha256,
    )
    mime = normalize_media_type(decision.descriptor.media_type)
    if decision.modality == "image":
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime};base64,{encoded}",
                "detail": decision.detail,
            },
        }
    if decision.modality == "audio":
        fmt = audio_format_of(mime)
        if fmt is not None:
            return {
                "type": "input_audio",
                "input_audio": {"data": encoded, "format": fmt},
            }
    if decision.modality == "pdf":
        return {
            "type": "file",
            "file": {
                "filename": decision.descriptor.name,
                "file_data": f"data:{mime};base64,{encoded}",
            },
        }
    return {"type": "text", "text": decision.reference_text()}


def _user_content(text, decisions):
    if not decisions:
        return text
    if any(decision.is_native for decision in decisions):
        parts = []
        if text:
            parts.append({"type": "text", "text": text})
        for decision in decisions:
            if decision.is_native:
                parts.append(_media_part(decision))
            else:
                parts.append({"type": "text", "text": decision.reference_text()})
        return parts
    refs = [decision.reference_text() for decision in decisions]
    return "\n\n".join(filter(None, (text, *refs)))


def _build_messages(conversation, projections, plan, *, route=None):
    found = []
    call_names = {}
    for entry, calls, results in tool_batches(
        conversation, lambda node: projections[id(node)].tools,
    ):
        projected = projections[id(entry)]
        passages = projected.passages
        entry_decisions = list(plan.for_entry(entry))
        if projected.assistant or calls:
            for call in calls:
                call_names[entries.call_id(call)] = call.get("name") or "tool"
            # Fold non-text assistant passages (e.g. hosted outputs) into the
            # single assistant message so we never emit empty or consecutive
            # assistant messages.
            assistant_texts = [
                text for role, text in passages if role == "assistant" and text
            ]
            if entry_decisions:
                assistant_texts.extend(d.reference_text() for d in entry_decisions)
                entry_decisions = []
            content = "\n\n".join(
                filter(None, (projected.content, *assistant_texts))
            )
            found.append(assistant_message(entry, calls, route, content))
            passages = tuple(
                (role, text) for role, text in passages if role != "assistant"
            )
        followup_parts = []
        for result, call_id in results:
            res_decisions = plan.for_tool(result)
            if not res_decisions:
                found.append({"role": "tool", "tool_call_id": call_id,
                              "content": tool_output(result)})
                continue
            base = tool_output(result, include_attachments=False)
            tname = result.get("tool") or call_names.get(call_id) or "tool"
            refs = []
            had_native = False
            for decision in res_decisions:
                if decision.is_native:
                    had_native = True
                    followup_parts.append({
                        "type": "text",
                        "text": (
                            f"[Tool output attachment from {tname} "
                            f"(call {call_id}): {decision.descriptor.name}]"
                        ),
                    })
                    followup_parts.append(_media_part(decision))
                else:
                    refs.append(decision.reference_text())
            content_text = "\n\n".join(filter(None, (base, *refs)))
            if not content_text and had_native:
                content_text = (
                    f"[Tool output attachment delivered in follow-up message "
                    f"for call {call_id}.]"
                )
            found.append({
                "role": "tool", "tool_call_id": call_id, "content": content_text,
            })
        # Emit ONE attributed synthetic user message immediately after the
        # complete adjacent role="tool" batch for supported tool-returned media.
        if followup_parts:
            found.append({"role": "user", "content": followup_parts})
        attached = False
        for role, text in passages:
            if role == "system" and found:
                role = "user"
                if not any(entries.has(entry, trait) for trait in (
                    "notice", "note", "rewind", "switch", "compaction_notice",
                )):
                    text = f"[Session note]\n{text}"
            if not attached and entry_decisions and role == "user":
                found.append({
                    "role": "user", "content": _user_content(text, entry_decisions),
                })
                attached = True
            else:
                found.append({"role": role, "content": text})
        if entry_decisions and not attached:
            found.append({
                "role": "user", "content": _user_content("", entry_decisions),
            })
    # An old compaction can retain a conversation starting at the model's turn.
    first_turn = int(bool(found and found[0]["role"] == "system"))
    if len(found) > first_turn and found[first_turn]["role"] == "assistant":
        found.insert(first_turn, {"role": "user", "content": ""})
    return found


def messages(conversation, *, route=None, project=None, capabilities=None,
             media_budget=None, plan=None):
    """Messages in the order this API requires for a tool round trip."""
    projections = (project or project_entries)(conversation)
    if plan is None:
        plan = plan_delivery(
            conversation,
            projections,
            api_name=NAME,
            capabilities=capabilities,
            media_budget=media_budget,
        )
    return _build_messages(conversation, projections, plan, route=route)


def _request_fields(model, wire_messages, *, tools=None, tool_choice=None,
                    effort=None, json_schema=None, json_mode=False,
                    temperature=None, max_output_tokens=None,
                    prefill_progress=False, thinking_field=None):
    fields = {
        "model": model,
        "messages": wire_messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        fields["tools"] = tools
        if tool_choice is not None:
            fields["tool_choice"] = tool_choice
    if effort is not None and thinking_field == "reasoning":
        fields["reasoning"] = {"effort": EFFORTS.get(effort, effort)}
    elif effort == "off" and thinking_field == "thinking":
        fields["thinking"] = {"type": "disabled"}
    elif effort == "off" and thinking_field == "enable_thinking":
        fields["enable_thinking"] = False
    elif effort is not None:
        fields["reasoning_effort"] = EFFORTS.get(effort, effort)
    if json_schema is not None:
        fields["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": json_schema["name"], "strict": True,
                            "schema": json_schema["schema"]},
        }
    elif json_mode:
        fields["response_format"] = {"type": "json_object"}
    if temperature is not None:
        fields["temperature"] = temperature
    if max_output_tokens is not None:
        fields["max_completion_tokens"] = max_output_tokens
    if prefill_progress:
        fields["extra_body"] = {"return_progress": True}
    return fields


def prepare_request(model, conversation, *, tools=None, tool_choice=None,
                    effort=None, json_schema=None, json_mode=False,
                    temperature=None, max_output_tokens=None,
                    prefill_progress=False, thinking_field=None, route=None,
                    project=None, capabilities=None, media_budget=None,
                    total_budget=None, client=None):
    """Prepare the exact Chat Completions body and accounting plan."""
    projections = (project or project_entries)(conversation)

    def build_body(plan):
        wire_messages = _build_messages(
            conversation, projections, plan, route=route,
        )
        return _request_fields(
            model, wire_messages,
            tools=tools, tool_choice=tool_choice, effort=effort,
            json_schema=json_schema, json_mode=json_mode,
            temperature=temperature, max_output_tokens=max_output_tokens,
            prefill_progress=prefill_progress, thinking_field=thinking_field,
        )

    return prepare_with_native_count(
        conversation,
        projections,
        api_name=NAME,
        capabilities=capabilities,
        media_budget=media_budget,
        total_budget=total_budget,
        route=route,
        build_body=build_body,
        native_counter=None,
    )


def request(model, conversation, *, tools=None, tool_choice=None, effort=None,
            json_schema=None, json_mode=False, temperature=None,
            max_output_tokens=None, prefill_progress=False, thinking_field=None,
            route=None, project=None, capabilities=None, media_budget=None,
            total_budget=None, client=None):
    """The request's fields. `json_schema`: {"name", "schema"}, strict JSON;
    `json_mode`: any JSON object; `prefill_progress`: llama.cpp's progress
    chunks while it reads the prompt."""
    return prepare_request(
        model, conversation,
        tools=tools, tool_choice=tool_choice, effort=effort,
        json_schema=json_schema, json_mode=json_mode,
        temperature=temperature, max_output_tokens=max_output_tokens,
        prefill_progress=prefill_progress, thinking_field=thinking_field,
        route=route, project=project, capabilities=capabilities,
        media_budget=media_budget, total_budget=total_budget,
        client=client,
    ).body


def request_options(server, *, encrypted=False, zero_temperature=False):
    """Adapter request kwargs implied by a server's declared capabilities."""
    options = {}
    if server is not None and server.chat_thinking_field:
        options["thinking_field"] = server.chat_thinking_field
    if zero_temperature and (server is None or server.accepts_zero_temperature):
        options["temperature"] = 0
    return options


def keeps_continuation(server):
    """Whether to persist route-scoped continuation items for `server`."""
    return True


def open_stream(client, model, messages, **standard):
    """Send prepared JSON; the SDK handles HTTP and parses streamed chunks."""
    prepared_body = standard.pop("prepared_body", None)
    body = (
        dict(prepared_body) if prepared_body is not None
        else request(model, messages, **standard)
    )
    body.update(body.pop("extra_body", {}))
    return client.post(
        "/chat/completions", body=body, cast_to=ChatCompletion,
        stream=True, stream_cls=Stream[ChatCompletionChunk],
    )


def runtime_context_window(data):
    """The effective window and whether loading can reveal it."""
    meta = data.get("meta")
    window = meta.get("n_ctx") if isinstance(meta, dict) else None
    if type(window) is int and window > 0:
        return window, True
    # The router can list the model's trained length before it is loaded.
    # That value can exceed the instance's configured window.
    return None, data.get("owned_by") == "llamacpp"


def alive(chunk):
    """Whether a chunk that carried nothing is still a sign of life: llama.cpp's
    prompt progress, sent while it reads a long prompt."""
    return bool((getattr(chunk, "model_extra", None) or {}).get("prompt_progress"))


def fold_reasoning_details(draft, details):
    items = draft.continuation_items.setdefault("reasoning_detail", {})
    for value in details:
        if not isinstance(value, dict):
            continue
        index = value.get("index")
        if type(index) is not int:
            index = len(items)
        entry = items.setdefault(index, {"item": {}})
        item = entry["item"]
        for name, data in value.items():
            if data is None and name in item:
                continue
            if (name in ("text", "summary", "data", "signature")
                    and isinstance(data, str) and isinstance(item.get(name), str)):
                item[name] += data
            else:
                item[name] = data


def _record_unrecognized(draft, item):
    slot = max(draft.unrecognized_outputs, default=-1) + 1
    draft.unrecognized_outputs[slot] = item


def _fold_content_blocks(draft, blocks):
    heard = False
    for raw in blocks:
        part = (
            raw.model_dump(mode="json") if hasattr(raw, "model_dump")
            else dict(raw) if isinstance(raw, Mapping) else None
        )
        if part is None:
            _record_unrecognized(draft, {"item": raw})
            continue
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text")
            if isinstance(text, str):
                if text:
                    draft.content += text
                    heard = True
            elif text is not None:
                _record_unrecognized(draft, part)
        elif ptype == "thinking":
            thinking = part.get("thinking")
            if isinstance(thinking, list):
                for sub in thinking:
                    if (isinstance(sub, Mapping) and sub.get("type") == "text"
                            and isinstance(sub.get("text"), str)):
                        if sub["text"]:
                            draft.reasoning += sub["text"]
                            heard = True
                    else:
                        _record_unrecognized(draft, part)
                        break
            elif isinstance(thinking, str):
                if thinking:
                    draft.reasoning += thinking
                    heard = True
            elif isinstance(part.get("text"), str):
                if part["text"]:
                    draft.reasoning += part["text"]
                    heard = True
            else:
                _record_unrecognized(draft, part)
        elif ptype == "reasoning":
            text = part.get("text")
            if text is None:
                text = part.get("reasoning")
            if isinstance(text, str):
                if text:
                    draft.reasoning += text
                    heard = True
            else:
                _record_unrecognized(draft, part)
        else:
            _record_unrecognized(draft, part)
    return heard


def _tool_call_slot(draft, piece):
    slots_by_id = draft.stream_state.setdefault("slots_by_id", {})
    slots_by_wire = draft.stream_state.setdefault("slots_by_wire_index", {})
    wire_index = piece.index if type(piece.index) is int else None
    last_slot = draft.stream_state.get("last_slot")
    piece_id = piece.id or ""
    if piece_id:
        if piece_id in slots_by_id:
            slot = slots_by_id[piece_id]
        elif wire_index is not None and (
            wire_index not in draft.calls or not draft.calls[wire_index]["id"]
        ):
            slot = wire_index
        elif (wire_index is None and last_slot in draft.calls
              and not draft.calls[last_slot]["id"]):
            slot = last_slot
        else:
            slot = max(draft.calls, default=-1) + 1
        slots_by_id[piece_id] = slot
        if wire_index is not None:
            slots_by_wire[wire_index] = slot
        draft.stream_state["last_slot"] = slot
        return slot
    if wire_index is not None:
        slot = slots_by_wire.get(wire_index, wire_index)
    else:
        slot = last_slot
        if slot is None:
            slot = max(draft.calls, default=-1) + 1
    draft.stream_state["last_slot"] = slot
    return slot


def remember_call_fields(draft, piece, call, slot):
    fields = piece.model_dump(mode="json", exclude_unset=True)
    extra = {key: value for key, value in fields.items()
             if key not in ("index", "id", "type", "function")}
    if fields.get("type") not in (None, "function"):
        extra["type"] = fields["type"]
    function = fields.get("function") or {}
    function_extra = {key: value for key, value in function.items()
                      if key not in ("name", "arguments")}
    if function_extra:
        extra["function"] = function_extra
    signatures = draft.continuation_items.get("thought_signature")
    extra_content = extra.get("extra_content")
    if isinstance(extra_content, dict) and extra_content.keys() == {"google"}:
        google = extra_content["google"]
        if (isinstance(google, dict) and google.keys() == {"thought_signature"}
                and entries.nonempty(google["thought_signature"])):
            signatures = draft.continuation_items.setdefault(
                "thought_signature", {},
            )
            signatures.setdefault(slot, {})["extra_content"] = extra.pop(
                "extra_content",
            )
    top_signature = extra.get("thought_signature")
    if entries.nonempty(top_signature):
        signatures = draft.continuation_items.setdefault("thought_signature", {})
        signatures.setdefault(slot, {})["thought_signature"] = extra.pop(
            "thought_signature",
        )
    if signatures is not None and slot in signatures:
        signature = signatures[slot]
        if call["id"] is not None:
            signature["call_id"] = call["id"]
        signature["name"] = call["name"]
        signature["arguments"] = call["arguments"]
    if extra:
        call.setdefault("wire_fields", {}).update(extra)


def fold(draft, chunk):
    """One chunk into the draft; True if it carried anything (a keep-alive
    or an empty delta doesn't, and so never resets the silence clock).
    Raises CutShort for a finish reason the API doesn't name."""
    heard = False
    usage = getattr(chunk, "usage", None)
    if usage is not None:
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", None) or 0
        draft.tokens = (usage.prompt_tokens, cached,
                        usage.completion_tokens or 0)
        by_modality = extract_detail_modality_tokens(
            usage, "prompt_tokens_details",
        )
        if by_modality:
            draft.input_tokens_by_modality = by_modality
        heard = True
    for choice in chunk.choices or []:
        delta = choice.delta
        if delta is not None:
            if isinstance(delta.content, str):
                if delta.content:
                    draft.content += delta.content
                    heard = True
            elif isinstance(delta.content, list):
                if _fold_content_blocks(draft, delta.content):
                    heard = True
            elif delta.content is not None:
                _record_unrecognized(draft, {"content": delta.content})
            extra = delta.model_extra or {}
            reasoning = extra.get("reasoning_content") or extra.get("reasoning")
            if reasoning:
                draft.reasoning += reasoning
                heard = True
            details = extra.get("reasoning_details")
            if isinstance(details, list) and details:
                fold_reasoning_details(draft, details)
                heard = True
            for piece in delta.tool_calls or []:
                slot = _tool_call_slot(draft, piece)
                call = draft.call(slot)
                if piece.id:
                    call["id"] = piece.id
                if piece.function is not None:
                    call["name"] += piece.function.name or ""
                    call["arguments"] += piece.function.arguments or ""
                remember_call_fields(draft, piece, call, slot)
                heard = True
        if choice.finish_reason is not None:
            reason = FINISHED.get(choice.finish_reason)
            if reason is None:
                raise CutShort(
                    f"the reply didn't complete ({choice.finish_reason})"
                )
            if reason == "stop" and draft.calls:
                reason = "tool_calls"
            draft.finish_reason = reason
            heard = True
    return heard
