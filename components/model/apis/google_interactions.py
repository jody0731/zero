"""Google's native Interactions API (`POST /v1beta/interactions`).

ZERO keeps no state on the server (`store` false): every request carries the
full conversation as flat chronological steps (`user_input`, `thought`,
`model_output`, `function_call`, `function_result`) and the leading system
prompt as `system_instruction`. Authentication uses the `x-goog-api-key` header
over `httpx2`, with no provider SDK dependency.
"""

import json
import math
from collections.abc import Mapping
from threading import Lock

import httpx2

from core import entries
from core.llm import (
    CutShort,
    ModelConnectionError,
    ModelHTTPError,
    ModelTimeoutError,
    StreamFailure,
    error_body_fields,
    tool_timeline,
)
from core.replay import tool_output
from components.model.apis.conversation import (
    project_entries,
    provider_items,
    reasoning,
)
from components.model.apis.multimodal import (
    TokenEstimate,
    normalize_media_type,
    plan_delivery,
    prepare_with_native_count,
    read_base64,
    request_hash,
)
from components.model.apis.search import (
    CitationSpan,
    SearchError,
    SearchResponse,
    SearchSuggestions,
    Source,
    is_http_url,
    post_json,
)
from components.model.limits import TokenLimits, model_id, token_count
from components.model.usage import normalize_modality_tokens

NAME = "google_interactions"
TITLE = "the Google Interactions API"
FEATURES = ("thinking_off", "thinking_on", "strict_json", "json_mode")
MIN_OUTPUT_TOKENS = 1
RETRY_V1_PREFIX = False
EFFORTS = {"off": "minimal", "low": "low", "high": "high", "max": "xhigh"}

NO_KEY = "none"
MAX_LIST_PAGES = 20
CALL_STEP_FIELDS = frozenset({"type", "id", "name", "arguments", "signature"})
# Provider-executed Interactions steps folded as non-executable hosted outputs.
HOSTED_STEP_KINDS = frozenset({
    "google_search_call",
    "google_search_result",
    "code_execution_call",
    "code_execution_result",
    "url_context_call",
    "url_context_result",
})
# Steps carrying an explicit `execution` discriminator ("server" vs "client").
EXECUTION_SCOPED_STEP_KINDS = frozenset({
    "tool_search_call",
    "tool_search_output",
})
HOSTED_STEP_OUTPUT_FIELDS = (
    "arguments", "result", "queries", "urls", "code", "language", "output",
    "is_error", "tools",
)
SCHEMA_MAPS = frozenset({
    "properties",
    "patternProperties",
    "$defs",
    "definitions",
    "dependentSchemas",
    "dependencies",
})
SCHEMA_VALUES = frozenset({
    "const",
    "default",
    "enum",
    "examples",
    "dependentRequired",
})


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _normalize_schema_node(schema):
    if isinstance(schema, list):
        return [_normalize_schema_node(item) for item in schema]
    if not isinstance(schema, Mapping):
        return schema
    properties = (
        schema.get("properties")
        if isinstance(schema.get("properties"), Mapping)
        else None
    )
    out = {}
    for key, value in schema.items():
        if key in SCHEMA_VALUES:
            out[key] = value
        elif key in SCHEMA_MAPS and isinstance(value, Mapping):
            out[key] = {k: _normalize_schema_node(v) for k, v in value.items()}
        # `required` may only name declared properties.
        elif key == "required" and properties is not None and isinstance(value, list):
            out[key] = [
                name for name in value
                if isinstance(name, str) and name in properties
            ]
        # Draft-04 boolean exclusive bounds become the numeric form.
        elif key == "exclusiveMinimum" and isinstance(value, bool):
            if value and _is_number(schema.get("minimum")):
                out[key] = schema["minimum"]
        elif key == "exclusiveMaximum" and isinstance(value, bool):
            if value and _is_number(schema.get("maximum")):
                out[key] = schema["maximum"]
        elif key == "minimum" and schema.get("exclusiveMinimum") is True:
            continue
        elif key == "maximum" and schema.get("exclusiveMaximum") is True:
            continue
        # Draft-07 tuples (`items` array + `additionalItems`) become 2020-12 form.
        elif key == "items" and isinstance(value, list):
            out["prefixItems"] = [_normalize_schema_node(item) for item in value]
        elif key == "additionalItems" and isinstance(schema.get("items"), list):
            out["items"] = _normalize_schema_node(value)
        else:
            out[key] = _normalize_schema_node(value)
    return out


def _cut_schema_loops(schema, target, safe):
    if isinstance(schema, list):
        return [_cut_schema_loops(item, target, safe) for item in schema]
    if not isinstance(schema, Mapping):
        return schema
    if schema.get("$ref") == target and not safe:
        return {}
    req_val = schema.get("required")
    required = req_val if isinstance(req_val, list) else ()
    min_items = schema.get("minItems")
    nonempty_array = _is_number(min_items) and min_items > 0
    out = {}
    for key, value in schema.items():
        if key in SCHEMA_VALUES:
            out[key] = value
        elif key == "items":
            out[key] = _cut_schema_loops(value, target, safe or not nonempty_array)
        elif key == "properties" and isinstance(value, Mapping):
            out[key] = {
                name: _cut_schema_loops(child, target, safe or name not in required)
                for name, child in value.items()
            }
        elif key in SCHEMA_MAPS and isinstance(value, Mapping):
            out[key] = {
                name: _cut_schema_loops(child, target, safe)
                for name, child in value.items()
            }
        else:
            out[key] = _cut_schema_loops(value, target, safe)
    return out


def normalize_gemini_schema(schema):
    """Rewrite JSON Schema shapes that Gemini rejects while preserving the rest."""
    normalized = _normalize_schema_node(schema)
    if not isinstance(normalized, dict):
        return {}
    trimmed = {}
    for key, value in normalized.items():
        if key in ("$defs", "definitions") and isinstance(value, Mapping):
            trimmed[key] = {
                name: _cut_schema_loops(definition, f"#/{key}/{name}", False)
                for name, definition in value.items()
            }
        else:
            trimmed[key] = value
    result = _cut_schema_loops(trimmed, "#", False)
    return result if isinstance(result, dict) else {}


def _http_error(response):
    raw = response.content
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        body = None
    code, message = error_body_fields(body)
    return ModelHTTPError(
        response.status_code,
        provider_code=code,
        provider_message=message,
        retry_after=response.headers.get("retry-after"),
        body=body,
    )


class InteractionsStream:
    """Thread-safe SSE stream wrapper whose `.close()` aborts the HTTP response."""

    def __init__(self, response):
        self._response = response
        self._closed = False

    def close(self):
        if not self._closed:
            self._closed = True
            self._response.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __iter__(self):
        try:
            yield from _iter_sse_events(self._response)
        except httpx2.TimeoutException as error:
            raise ModelTimeoutError(str(error)) from error
        except httpx2.HTTPError as error:
            if not self._closed:
                raise ModelConnectionError(str(error)) from error
        finally:
            self.close()


def _parse_sse_payload(event_name, data_lines):
    if not data_lines:
        return None
    payload = "\n".join(data_lines)
    if event_name == "done" or payload == "[DONE]" or not payload.strip():
        return None
    try:
        parsed = json.loads(payload)
    except ValueError as error:
        raise CutShort("malformed JSON event in interaction stream") from error
    if not isinstance(parsed, dict):
        raise CutShort("interaction stream event must be a JSON object")
    return parsed


def _iter_sse_events(response):
    event_name = None
    data_lines = []
    for line in response.iter_lines():
        if line == "":
            parsed = _parse_sse_payload(event_name, data_lines)
            event_name = None
            data_lines = []
            if parsed is not None:
                yield parsed
            continue
        field, sep, value = line.partition(":")
        if not sep or not field:
            continue
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value.strip()
        elif field == "data":
            data_lines.append(value)
    parsed = _parse_sse_payload(event_name, data_lines)
    if parsed is not None:
        yield parsed


class InteractionsClient:
    """Context-manageable `httpx2` transport for the Interactions API."""

    def __init__(self, url, key, timeout):
        self.base_url = url.rstrip("/")
        self.api_key = None if not key or key == NO_KEY else key
        self.timeout = timeout
        self._http = httpx2.Client(timeout=timeout, follow_redirects=False)

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def headers(self):
        if self.api_key:
            return {"x-goog-api-key": self.api_key}
        return {}

    def get_json(self, path, *, params=None):
        try:
            response = self._http.get(
                f"{self.base_url}{path}", headers=self.headers(), params=params,
            )
        except httpx2.TimeoutException as error:
            raise ModelTimeoutError(str(error)) from error
        except httpx2.HTTPError as error:
            raise ModelConnectionError(str(error)) from error
        if response.status_code >= 400:
            raise _http_error(response)
        try:
            return response.json()
        except ValueError as error:
            raise ModelConnectionError("invalid JSON response from model server") \
                from error

    def post_json(self, path, body):
        try:
            response = self._http.post(
                f"{self.base_url}{path}", json=body, headers=self.headers(),
            )
        except httpx2.TimeoutException as error:
            raise ModelTimeoutError(str(error)) from error
        except httpx2.HTTPError as error:
            raise ModelConnectionError(str(error)) from error
        if response.status_code >= 400:
            raise _http_error(response)
        try:
            return response.json()
        except ValueError as error:
            raise ModelConnectionError("invalid JSON response from model server") \
                from error

    def post_stream(self, path, body):
        req = self._http.build_request(
            "POST", f"{self.base_url}{path}", json=body, headers=self.headers(),
        )
        try:
            response = self._http.send(req, stream=True)
        except httpx2.TimeoutException as error:
            raise ModelTimeoutError(str(error)) from error
        except httpx2.HTTPError as error:
            raise ModelConnectionError(str(error)) from error
        if response.status_code >= 400:
            try:
                response.read()
            except httpx2.HTTPError:
                pass
            finally:
                response.close()
            raise _http_error(response)
        return InteractionsStream(response)


def make_client(url, key, timeout):
    """Create a context-manageable client for this API."""
    return InteractionsClient(url, key, timeout)


def list_models(url, key, timeout):
    """List native models with supported generation methods and token limits."""
    found = []
    seen_tokens = set()
    page_token = None
    with make_client(url, key, timeout) as client:
        for _ in range(MAX_LIST_PAGES):
            params = {"pageToken": page_token} if page_token else None
            payload = client.get_json("/models", params=params)
            if not isinstance(payload, dict):
                break
            for item in payload.get("models") or ():
                if not isinstance(item, dict):
                    continue
                methods = [
                    method for method in (item.get("supportedGenerationMethods") or ())
                    if isinstance(method, str) and method
                ]
                raw_name = item.get("name")
                if not isinstance(raw_name, str):
                    continue
                name = raw_name.removeprefix("models/")
                if not model_id(name):
                    continue
                input_limit = token_count(item.get("inputTokenLimit"))
                output_limit = token_count(item.get("outputTokenLimit"))
                limits = TokenLimits.read({
                    "context_window": input_limit,
                    "max_input_tokens": input_limit,
                    "max_output_tokens": output_limit,
                })
                row = {
                    "id": name,
                    "context_window": limits.context_window,
                    **limits.as_dict(),
                    "supported_methods": methods,
                    "reply_capable": "generateContent" in methods,
                }
                if isinstance(item.get("thinking"), bool):
                    row["reasoning"] = item["thinking"]
                    row["capabilities"] = {"reasoning": item["thinking"]}
                found.append(row)
            next_token = payload.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                break
            if next_token in seen_tokens:
                break
            seen_tokens.add(next_token)
            page_token = next_token
    return found


def _parse_call_arguments(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def classify_step(step):
    """Classify an Interactions step by execution ownership and kind."""
    if not isinstance(step, Mapping):
        return "unsupported"
    kind = step.get("type")
    if kind in ("thought", "model_output", "function_call"):
        return "core"
    status = step.get("status")
    if kind in HOSTED_STEP_KINDS:
        if step.get("execution") not in (None, "server"):
            return "unsupported"
        if status in (None, "completed"):
            return "hosted_completed"
        if status == "in_progress":
            return "hosted_pending"
        return "unsupported"
    if kind in EXECUTION_SCOPED_STEP_KINDS:
        if step.get("execution") != "server":
            return "unsupported"
        if status in (None, "completed"):
            return "hosted_completed"
        if status == "in_progress":
            return "hosted_pending"
        return "unsupported"
    return "unsupported"


def is_completed_hosted_step(step):
    return classify_step(step) == "hosted_completed"


def _hosted_step_summary(kind, item):
    args = item.get("arguments")
    if kind == "google_search_call":
        queries = (args.get("queries") if isinstance(args, Mapping) else None) or (
            item.get("queries")
        )
        if isinstance(queries, list) and queries:
            return f"[Hosted google_search_call: {', '.join(map(str, queries))}]"
    elif kind == "url_context_call":
        urls = (args.get("urls") if isinstance(args, Mapping) else None) or (
            item.get("urls")
        )
        if isinstance(urls, list) and urls:
            return f"[Hosted url_context_call: {', '.join(map(str, urls))}]"
    return f"[Hosted {kind}: completed]"


def _hosted_step_fields(item):
    kind = item["type"]
    fields = {
        "kind": kind,
        "execution": "server",
        "status": "completed",
        "summary": _hosted_step_summary(kind, item),
    }
    if isinstance(item_id := item.get("id"), str) and item_id:
        fields["item_id"] = item_id
    if isinstance(call_id := item.get("call_id"), str) and call_id:
        fields["call_id"] = call_id
    output = {
        key: item[key] for key in HOSTED_STEP_OUTPUT_FIELDS
        if key in item and item[key] is not None
    }
    if output:
        fields["output"] = output
    return fields


def _hosted_step_key(item):
    kind = item.get("type")
    if isinstance(item_id := item.get("id"), str) and item_id:
        return (kind, "id", item_id)
    if isinstance(call_id := item.get("call_id"), str) and call_id:
        return (kind, "call_id", call_id)
    return None


def _record_hosted_step(draft, index, item):
    key = _hosted_step_key(item)
    seen = draft.stream_state.setdefault("hosted_step_keys", {})
    slot = seen.get(key, index) if key is not None else index
    if key is not None:
        seen[key] = slot
    draft.unrecognized_outputs.pop(index, None)
    draft.unrecognized_outputs.pop(slot, None)
    stored = draft.continuation_items.setdefault("interaction_step", {})
    previous = stored.get(slot, {}).get("item")
    if isinstance(previous, Mapping) and previous.get("type") == item.get("type"):
        item = {**previous, **item}
    draft.hosted_outputs[slot] = _hosted_step_fields(item)
    stored[slot] = {"item": item}


def interaction_steps(entry, route, content, calls):
    """Route-scoped continuation steps still matching the public reply and calls."""
    stored = [
        item for node in provider_items(entry, route, "interaction_step")
        if isinstance(item := node.get("item"), Mapping) and "type" in item
    ]
    native_calls = {
        item["id"]: (item["name"], item.get("arguments"))
        for item in stored if item["type"] == "function_call"
    }
    expected = {
        entries.call_id(call): (
            call.get("name"), _parse_call_arguments(call.get("arguments")),
        )
        for call in calls
    }
    if native_calls and native_calls != expected:
        return []
    native_outputs = [item for item in stored if item["type"] == "model_output"]
    keep_text = bool(content)
    if native_outputs and keep_text:
        parts = [
            part for item in native_outputs for part in item.get("content", ())
        ]
        keep_text = (
            all(isinstance(p, Mapping) and p.get("type") == "text" for p in parts)
            and "".join(p["text"] for p in parts) == content
        )
    return [
        entries.thaw(item) for item in stored
        if item["type"] in ("thought", "function_call")
        or is_completed_hosted_step(item)
        or (item["type"] == "model_output" and keep_text)
    ]


def assistant_steps(entry, route, calls, content):
    replayed = interaction_steps(entry, route, content, calls)
    yield from replayed
    kinds = {item["type"] for item in replayed}
    thought = reasoning(entry, route)
    if "thought" not in kinds and thought:
        yield {"type": "thought", "summary": [{"type": "text", "text": thought}]}
    if "model_output" not in kinds and content:
        yield {"type": "model_output", "content": [{"type": "text", "text": content}]}
    replayed_calls = {
        item["id"] for item in replayed if item["type"] == "function_call"
    }
    for call in calls:
        cid = entries.call_id(call)
        if cid not in replayed_calls:
            yield {
                "type": "function_call",
                "id": cid,
                "name": call.get("name"),
                "arguments": _parse_call_arguments(call.get("arguments")),
            }


_COUNT_TOKENS_LOCK = Lock()
_COUNT_TOKENS_CACHE = {}
_NO_COUNT_TOKENS_ROUTES = set()
_COUNT_TOKENS_CACHE_MAX = 512
_COUNT_MIN_MARGIN_TOKENS = 64
_COUNT_MARGIN_RATIO = 0.05
_MEDIA_PART_KINDS = frozenset({"image", "audio", "video", "document"})


def clear_count_tokens_cache():
    """Clear cached Gemini :countTokens counts and 404 memoization."""
    with _COUNT_TOKENS_LOCK:
        _COUNT_TOKENS_CACHE.clear()
        _NO_COUNT_TOKENS_ROUTES.clear()


def _count_route_key(client, route):
    if route:
        return str(route)
    base = getattr(client, "base_url", None)
    return str(base) if base is not None else str(id(client))


def _media_part(decision):
    encoded = read_base64(
        decision.blob_path, expected_sha256=decision.descriptor.sha256,
    )
    mime = normalize_media_type(decision.descriptor.media_type)
    kind = "document" if decision.modality == "pdf" else decision.modality
    return {"type": kind, "data": encoded, "mime_type": mime}


def _user_content_parts(text, decisions):
    parts = []
    if text:
        parts.append({"type": "text", "text": text})
    for decision in decisions:
        if decision.is_native:
            parts.append(_media_part(decision))
        else:
            parts.append({"type": "text", "text": decision.reference_text()})
    return parts


def _tool_result_wire(result, decisions):
    if not decisions:
        output = tool_output(result)
        return "" if output is None else output
    base = tool_output(result, include_attachments=False)
    if any(decision.is_native for decision in decisions):
        parts = []
        if base:
            parts.append({"type": "text", "text": base})
        for decision in decisions:
            if decision.is_native:
                parts.append(_media_part(decision))
            else:
                parts.append({"type": "text", "text": decision.reference_text()})
        return parts
    refs = [decision.reference_text() for decision in decisions]
    return "\n\n".join(filter(None, (base, *refs)))


def _translate_content_parts(raw_parts):
    parts = []
    omitted = 0
    for item in raw_parts or ():
        if not isinstance(item, Mapping):
            continue
        kind = item.get("type")
        if kind == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append({"text": text})
        elif kind in _MEDIA_PART_KINDS:
            data = item.get("data")
            mime = item.get("mime_type")
            if isinstance(data, str) and isinstance(mime, str):
                parts.append({"inlineData": {"mimeType": mime, "data": data}})
        else:
            omitted += len(json.dumps(item, ensure_ascii=False)) // 2 + 16
    return parts, omitted


def interactions_to_generate_content(body):
    """Translate a prepared Interactions body into GenerateContentRequest.

    Returns `(gc_request, omitted_framing_tokens)` where `omitted_framing_tokens`
    conservatively bounds Interactions-only signatures, call IDs, and framing
    that `GenerateContentRequest` cannot carry verbatim.
    """
    gc = {}
    omitted = 0
    sys_text = body.get("system_instruction")
    if isinstance(sys_text, str) and sys_text:
        gc["systemInstruction"] = {"parts": [{"text": sys_text}]}
    raw_tools = body.get("tools") or ()
    declarations = []
    for item in raw_tools:
        if isinstance(item, Mapping) and item.get("type") == "function":
            declarations.append({
                "name": item.get("name", ""),
                "description": item.get("description", ""),
                "parameters": item.get("parameters", {}),
            })
        elif isinstance(item, Mapping):
            omitted += len(json.dumps(item, ensure_ascii=False)) // 2 + 32
    if declarations:
        gc["tools"] = [{"functionDeclarations": declarations}]
    contents = []

    def append_content(role, parts):
        if not parts:
            return
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": list(parts)})

    for step in body.get("input") or ():
        if not isinstance(step, Mapping):
            continue
        kind = step.get("type")
        if kind == "user_input":
            parts, extra = _translate_content_parts(step.get("content"))
            omitted += extra
            append_content("user", parts)
        elif kind == "model_output":
            parts, extra = _translate_content_parts(step.get("content"))
            omitted += extra
            append_content("model", parts)
        elif kind == "thought":
            parts, extra = _translate_content_parts(step.get("summary"))
            omitted += extra
            sig = step.get("signature")
            if isinstance(sig, str) and sig:
                omitted += max(16, len(sig) // 4)
            append_content("model", parts)
        elif kind == "function_call":
            sig = step.get("signature")
            if isinstance(sig, str) and sig:
                omitted += max(16, len(sig) // 4)
            omitted += 8
            append_content("model", [{
                "functionCall": {
                    "name": step.get("name") or "",
                    "args": step.get("arguments") or {},
                },
            }])
        elif kind == "function_result":
            omitted += 12
            name = step.get("name") or ""
            raw_res = step.get("result")
            if isinstance(raw_res, list):
                text_chunks = []
                media_parts = []
                for item in raw_res:
                    if not isinstance(item, Mapping):
                        continue
                    item_type = item.get("type")
                    if item_type == "text" and isinstance(item.get("text"), str):
                        text_chunks.append(item["text"])
                    elif item_type in _MEDIA_PART_KINDS:
                        data = item.get("data")
                        mime = item.get("mime_type")
                        if isinstance(data, str) and isinstance(mime, str):
                            media_parts.append({
                                "inlineData": {"mimeType": mime, "data": data},
                            })
                    else:
                        omitted += len(json.dumps(item, ensure_ascii=False)) // 2 + 16
                parts = [{
                    "functionResponse": {
                        "name": name,
                        "response": {"result": "\n\n".join(text_chunks)},
                    },
                }, *media_parts]
            else:
                parts = [{
                    "functionResponse": {
                        "name": name,
                        "response": {"result": "" if raw_res is None else str(raw_res)},
                    },
                }]
            append_content("user", parts)
        else:
            omitted += len(json.dumps(step, ensure_ascii=False)) // 2 + 16
    gc["contents"] = contents
    return gc, omitted


def count_input_tokens(client, body, *, route=None):
    """Count tokens via POST /v1beta/models/{model}:countTokens with margin.

    Translates the prepared Interactions plan into GenerateContentRequest and
    adds an explicit uncertainty margin for omitted Interactions framing,
    thought signatures, and tool metadata. Cached by `(history_route, plan_hash)`.
    """
    model_name = str(body.get("model") or "").removeprefix("models/")
    if not model_name:
        return None
    gc_body, omitted = interactions_to_generate_content(body)
    route_key = _count_route_key(client, route)
    digest = request_hash({"model": model_name, **gc_body})
    cache_key = (route_key, digest)
    with _COUNT_TOKENS_LOCK:
        if route_key in _NO_COUNT_TOKENS_ROUTES:
            return None
        cached = _COUNT_TOKENS_CACHE.get(cache_key)
        if cached is not None:
            return cached
    count_request = {
        "generateContentRequest": {
            "model": f"models/{model_name}",
            **gc_body,
        },
    }
    try:
        payload = client.post_json(
            f"/models/{model_name}:countTokens", count_request,
        )
    except ModelHTTPError as error:
        if error.status_code in (404, 405):
            with _COUNT_TOKENS_LOCK:
                _NO_COUNT_TOKENS_ROUTES.add(route_key)
        return None
    except (ModelConnectionError, ModelTimeoutError, OSError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    total = payload.get("totalTokens")
    if total is None:
        total = payload.get("total_tokens")
    if type(total) is not int or total < 0:
        return None
    margin = max(_COUNT_MIN_MARGIN_TOKENS, math.ceil(total * _COUNT_MARGIN_RATIO))
    margin += omitted
    estimate = TokenEstimate(
        tokens=total + margin,
        method="native_count_tokens",
        uncertainty=(
            f"translated countTokens ({total:,}) + {margin:,} token margin "
            "for Interactions framing/signatures/tools"
        ),
        native=True,
        margin_tokens=margin,
    )
    with _COUNT_TOKENS_LOCK:
        if len(_COUNT_TOKENS_CACHE) >= _COUNT_TOKENS_CACHE_MAX:
            _COUNT_TOKENS_CACHE.clear()
        _COUNT_TOKENS_CACHE[cache_key] = estimate
    return estimate


def _build_input_steps(conversation, projections, plan, *, route=None):
    system_instruction = None
    found = []
    call_names = {}
    timeline = tool_timeline(conversation, lambda node: projections[id(node)].tools)
    for index, (entry, calls, results, _) in enumerate(timeline):
        projected = projections[id(entry)]
        passages = projected.passages
        if (index == 0 and entry.origin == "system"
                and entries.has(entry, "system_prompt")):
            system_instruction = entries.project(entry)
            if system_instruction:
                passages = passages[1:]
        entry_decisions = list(plan.for_entry(entry))
        if projected.assistant or calls:
            for call in calls:
                call_names[entries.call_id(call)] = call.get("name") or ""
            emitted = list(assistant_steps(entry, route, calls, projected.content))
            if any(is_completed_hosted_step(item) for item in emitted):
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
            assistant_extra = [
                {"type": "model_output", "content": [{"type": "text", "text": text}]}
                for role, text in passages if role == "assistant"
            ]
            if entry_decisions:
                refs = "\n\n".join(d.reference_text() for d in entry_decisions)
                assistant_extra.append({
                    "type": "model_output",
                    "content": [{"type": "text", "text": refs}],
                })
                entry_decisions = []
            passages = tuple(
                (role, text) for role, text in passages if role != "assistant"
            )
            # Emit cross-route assistant passages inside the model turn before
            # function_call steps and function_result replies.
            call_idx = next(
                (i for i, step in enumerate(emitted)
                 if step.get("type") == "function_call"),
                len(emitted),
            )
            found.extend(emitted[:call_idx])
            found.extend(assistant_extra)
            found.extend(emitted[call_idx:])
        for result, cid in results:
            res_decisions = plan.for_tool(result)
            step = {
                "type": "function_result",
                "call_id": cid,
                "name": result.get("tool") or call_names.get(cid, ""),
                "result": _tool_result_wire(result, res_decisions),
            }
            if result.get("failed") or result.get("denied") or result.get("bad_call"):
                step["is_error"] = True
            found.append(step)
        attached = False
        for role, text in passages:
            step_type = "model_output" if role == "assistant" else "user_input"
            if not attached and entry_decisions and step_type == "user_input":
                found.append({
                    "type": "user_input",
                    "content": _user_content_parts(text, entry_decisions),
                })
                attached = True
            else:
                found.append({
                    "type": step_type,
                    "content": [{"type": "text", "text": text}],
                })
        if entry_decisions and not attached:
            found.append({
                "type": "user_input",
                "content": _user_content_parts("", entry_decisions),
            })
    return system_instruction, found


def input_steps(conversation, *, route=None, project=None, capabilities=None,
                media_budget=None, plan=None):
    """System instruction and flat chronological input steps from the timeline."""
    projections = (project or project_entries)(conversation)
    if plan is None:
        plan = plan_delivery(
            conversation,
            projections,
            api_name=NAME,
            capabilities=capabilities,
            media_budget=media_budget,
        )
    return _build_input_steps(conversation, projections, plan, route=route)


def tool(schema):
    """Convert a Chat-shaped tool schema to an Interactions function tool."""
    function = schema["function"]
    return {
        "type": "function",
        "name": function["name"],
        "description": function.get("description", ""),
        "parameters": normalize_gemini_schema(function.get("parameters", {})),
    }


def _request_fields(model, system_instruction, steps, *, tools=None,
                    tool_choice=None, effort=None, json_schema=None,
                    json_mode=False, temperature=None, max_output_tokens=None,
                    reasoning_summary=False):
    fields = {
        "model": model.removeprefix("models/"),
        "input": steps,
        "stream": True,
        "store": False,
    }
    if system_instruction is not None:
        fields["system_instruction"] = system_instruction
    if tools:
        fields["tools"] = [tool(schema) for schema in tools]
    generation = {}
    if tools and tool_choice is not None:
        generation["tool_choice"] = tool_choice
    if max_output_tokens is not None:
        generation["max_output_tokens"] = max_output_tokens
    if temperature is not None:
        generation["temperature"] = temperature
    if effort is not None:
        generation["thinking_level"] = EFFORTS.get(effort, effort)
    if reasoning_summary and effort != "off":
        generation["thinking_summaries"] = "auto"
    if generation:
        fields["generation_config"] = generation
    if json_schema is not None:
        fields["response_format"] = {
            "type": "text",
            "mime_type": "application/json",
            "schema": normalize_gemini_schema(json_schema["schema"]),
        }
    elif json_mode:
        fields["response_format"] = {
            "type": "text",
            "mime_type": "application/json",
        }
    return fields


def prepare_request(model, conversation, *, tools=None, tool_choice=None,
                    effort=None, json_schema=None, json_mode=False,
                    temperature=None, max_output_tokens=None,
                    reasoning_summary=False, route=None, project=None,
                    capabilities=None, media_budget=None, total_budget=None,
                    client=None):
    """Prepare the exact Interactions JSON body and accounting plan."""
    projections = (project or project_entries)(conversation)

    def build_body(plan):
        system_instruction, steps = _build_input_steps(
            conversation, projections, plan, route=route,
        )
        return _request_fields(
            model, system_instruction, steps,
            tools=tools, tool_choice=tool_choice, effort=effort,
            json_schema=json_schema, json_mode=json_mode,
            temperature=temperature, max_output_tokens=max_output_tokens,
            reasoning_summary=reasoning_summary,
        )

    native_counter = (
        (lambda body, *, route=None: count_input_tokens(client, body, route=route))
        if client is not None else None
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
        native_counter=native_counter,
    )


def request(model, conversation, *, tools=None, tool_choice=None, effort=None,
            json_schema=None, json_mode=False, temperature=None,
            max_output_tokens=None, reasoning_summary=False, route=None,
            project=None, capabilities=None, media_budget=None,
            total_budget=None, client=None):
    """Build the JSON request body for `POST /interactions`."""
    return prepare_request(
        model, conversation,
        tools=tools, tool_choice=tool_choice, effort=effort,
        json_schema=json_schema, json_mode=json_mode,
        temperature=temperature, max_output_tokens=max_output_tokens,
        reasoning_summary=reasoning_summary,
        route=route, project=project, capabilities=capabilities,
        media_budget=media_budget, total_budget=total_budget,
        client=client,
    ).body


def request_options(server, *, encrypted=False, zero_temperature=False):
    """Adapter request kwargs implied by a server's declared capabilities."""
    options = {}
    if server is not None and server.requests_reasoning_summary:
        options["reasoning_summary"] = True
    if zero_temperature and (server is None or server.accepts_zero_temperature):
        options["temperature"] = 0
    return options


def keeps_continuation(server):
    """Whether to persist route-scoped continuation items for `server`."""
    return True


def open_stream(client, model, conversation, **standard):
    """Open an SSE interaction stream on `client`."""
    prepared_body = standard.pop("prepared_body", None)
    body = (
        prepared_body if prepared_body is not None
        else request(model, conversation, **standard)
    )
    return client.post_stream("/interactions", body)


def _finalize_step(draft, index):
    steps = draft.stream_state.get("steps") or {}
    state = steps.get(index)
    if state is None or state.get("stopped"):
        return
    state["stopped"] = True
    kind = state["type"]
    item = None
    if kind == "thought":
        thought = {"type": "thought"}
        if state.get("signature"):
            thought["signature"] = state["signature"]
        summary_text = "".join(state["summary"])
        if summary_text:
            thought["summary"] = [{"type": "text", "text": summary_text}]
        if len(thought) > 1:
            item = thought
    elif kind == "model_output":
        full_text = "".join(state["text"])
        if full_text:
            item = {
                "type": "model_output",
                "content": [{"type": "text", "text": full_text}],
            }
    elif kind == "function_call":
        call = draft.call(index)
        if not call["arguments"]:
            call["arguments"] = "{}"
        item = {
            "type": "function_call",
            "id": call["id"],
            "name": call["name"],
            "arguments": _parse_call_arguments(call["arguments"]),
        }
        if state.get("signature"):
            item["signature"] = state["signature"]
    elif state.get("hosted"):
        hosted = dict(state["item"])
        if state.get("arguments_chunks"):
            raw = "".join(state["arguments_chunks"])
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = raw
            hosted["arguments"] = parsed
        if state.get("text_chunks"):
            text_val = "".join(state["text_chunks"])
            if kind.endswith("_result"):
                hosted["result"] = text_val
            else:
                hosted["text"] = text_val
        if state.get("signature"):
            hosted["signature"] = state["signature"]
        if is_completed_hosted_step(hosted):
            _record_hosted_step(draft, index, hosted)
        else:
            draft.hosted_outputs.pop(index, None)
            draft.unrecognized_outputs[index] = hosted
        return
    if item is not None:
        draft.continuation_items.setdefault("interaction_step", {})[index] = {
            "item": item,
        }


def _fold_step_start(draft, index, step):
    if not isinstance(index, int) or not isinstance(step, Mapping):
        slot = index if isinstance(index, int) else len(draft.unrecognized_outputs)
        draft.unrecognized_outputs[slot] = (
            dict(step) if isinstance(step, Mapping) else {"step": step}
        )
        return False
    steps = draft.stream_state.setdefault("steps", {})
    kind = step.get("type")
    if kind == "thought":
        state = {"type": "thought", "summary": [], "signature": step.get("signature")}
        heard = False
        for part in step.get("summary") or ():
            if (isinstance(part, Mapping) and part.get("type") == "text"
                    and isinstance(part.get("text"), str)):
                if part["text"]:
                    draft.reasoning += part["text"]
                    state["summary"].append(part["text"])
                    heard = True
            else:
                draft.unrecognized_outputs[index] = dict(step)
                return False
        steps[index] = state
        return heard
    if kind == "model_output":
        state = {"type": "model_output", "text": []}
        heard = False
        for part in step.get("content") or ():
            if (isinstance(part, Mapping) and part.get("type") == "text"
                    and isinstance(part.get("text"), str)):
                if part["text"]:
                    draft.content += part["text"]
                    state["text"].append(part["text"])
                    heard = True
            else:
                draft.unrecognized_outputs[index] = dict(step)
                return False
        steps[index] = state
        return heard
    if kind == "function_call":
        call = draft.call(index)
        call["id"] = step.get("id")
        call["name"] = step.get("name") or ""
        initial_params = step.get("arguments")
        if isinstance(initial_params, Mapping) and initial_params:
            call["arguments"] = json.dumps(initial_params, ensure_ascii=False)
        elif isinstance(initial_params, str) and initial_params:
            call["arguments"] = initial_params
        else:
            call["arguments"] = ""
        extra = {k: v for k, v in step.items() if k not in CALL_STEP_FIELDS}
        if extra:
            call.setdefault("wire_fields", {}).update(extra)
        steps[index] = {
            "type": "function_call",
            "id": call["id"],
            "name": call["name"],
            "signature": step.get("signature"),
        }
        return True
    category = classify_step(step)
    if category in ("hosted_completed", "hosted_pending"):
        steps[index] = {
            "type": kind,
            "hosted": True,
            "item": dict(step),
            "arguments_chunks": [],
            "text_chunks": [],
            "signature": step.get("signature"),
        }
        if category == "hosted_pending":
            draft.unrecognized_outputs[index] = dict(step)
        return True
    draft.unrecognized_outputs[index] = dict(step)
    return False


def _fold_step_delta(draft, index, delta):
    steps = draft.stream_state.get("steps") or {}
    state = steps.get(index)
    if state is None or not isinstance(delta, Mapping):
        slot = index if isinstance(index, int) else len(draft.unrecognized_outputs)
        draft.unrecognized_outputs[slot] = (
            dict(delta) if isinstance(delta, Mapping) else {"delta": delta}
        )
        return False
    dtype = delta.get("type")
    if dtype == "text" and state["type"] == "model_output":
        text = delta.get("text")
        if isinstance(text, str):
            if text:
                draft.content += text
                state["text"].append(text)
            return bool(text)
    elif dtype == "thought_summary" and state["type"] == "thought":
        content = delta.get("content")
        if (isinstance(content, Mapping) and content.get("type") == "text"
                and isinstance(content.get("text"), str)):
            text = content["text"]
            if text:
                draft.reasoning += text
                state["summary"].append(text)
            return bool(text)
    elif dtype == "thought_signature":
        sig = delta.get("signature")
        if isinstance(sig, str) and sig:
            state["signature"] = sig
            return True
        return False
    elif dtype == "arguments_delta" and state["type"] == "function_call":
        chunk = delta.get("arguments")
        if isinstance(chunk, str):
            draft.call(index)["arguments"] += chunk
            return bool(chunk)
    elif state.get("hosted"):
        if delta.get("execution") not in (None, "server"):
            draft.unrecognized_outputs[index] = dict(delta)
            return False
        if dtype == "arguments_delta" and isinstance(delta.get("arguments"), str):
            chunk = delta["arguments"]
            state["arguments_chunks"].append(chunk)
            return bool(chunk)
        if dtype == "text" and isinstance(delta.get("text"), str):
            chunk = delta["text"]
            state["text_chunks"].append(chunk)
            return bool(chunk)
        if dtype == state["type"]:
            for key, value in delta.items():
                if key != "type":
                    state["item"][key] = value
            return True
    draft.unrecognized_outputs[index] = dict(delta)
    return False


def _fold_terminal_steps(draft, interaction):
    terminal = interaction.get("steps")
    if terminal is None:
        terminal = interaction.get("outputs")
    if not isinstance(terminal, list):
        return
    streamed = draft.stream_state.get("steps") or {}
    known_calls = {call.get("id") for call in draft.calls.values() if call.get("id")}
    for index, step in enumerate(terminal):
        if not isinstance(step, Mapping):
            slot = max(draft.unrecognized_outputs, default=-1) + 1
            draft.unrecognized_outputs[slot] = {"step": step}
            continue
        category = classify_step(step)
        if category == "hosted_completed":
            _record_hosted_step(draft, index, dict(step))
        elif category == "core":
            if index in streamed:
                continue
            if step.get("type") == "function_call":
                cid = step.get("id")
                if cid and cid in known_calls:
                    continue
                slot = max((*draft.calls, *streamed, index - 1), default=-1) + 1
                _fold_step_start(draft, slot, step)
                _finalize_step(draft, slot)
        else:
            draft.hosted_outputs.pop(index, None)
            draft.unrecognized_outputs[index] = dict(step)


def _fold_usage(draft, interaction):
    used = interaction.get("usage")
    if not isinstance(used, Mapping):
        return
    in_tok = used.get("total_input_tokens")
    prompt = in_tok if in_tok is not None else (used.get("raw_prompt_token") or 0)
    cached = used.get("total_cached_tokens") or 0
    out = (used.get("total_output_tokens") or 0) + (
        used.get("total_thought_tokens") or 0
    )
    draft.tokens = (prompt, cached, out)
    by_modality = normalize_modality_tokens(
        used.get("input_tokens_by_modality"),
        used.get("total_input_tokens_by_modality"),
        used.get("prompt_tokens_details"),
        used.get("promptTokensDetails"),
    )
    if by_modality:
        draft.input_tokens_by_modality = by_modality


def fold(draft, event):
    """Fold one Interactions SSE event dict into `draft`."""
    if not isinstance(event, Mapping):
        return False
    event_type = event.get("event_type")
    if event_type == "step.start":
        return _fold_step_start(draft, event.get("index"), event.get("step"))
    if event_type == "step.delta":
        return _fold_step_delta(draft, event.get("index"), event.get("delta"))
    if event_type == "step.stop":
        index = event.get("index")
        _finalize_step(draft, index)
        return index in (draft.stream_state.get("steps") or {})
    if event_type == "interaction.completed":
        for index in list(draft.stream_state.get("steps") or ()):
            _finalize_step(draft, index)
        interaction = event.get("interaction")
        if not isinstance(interaction, Mapping):
            raise CutShort("interaction.completed missing interaction payload")
        status = interaction.get("status")
        if status in ("failed", "cancelled"):
            errors = interaction.get("errors") or ()
            code = next(
                (err.get("code") for err in errors
                 if isinstance(err, Mapping) and err.get("code")),
                None,
            )
            messages = [
                err["message"].strip() for err in errors
                if isinstance(err, Mapping)
                and isinstance(err.get("message"), str)
                and err["message"].strip()
            ]
            message = "\n".join(messages) if messages else f"interaction {status}"
            raise StreamFailure(code, message)
        _fold_terminal_steps(draft, interaction)
        _fold_usage(draft, interaction)
        if status == "requires_action":
            draft.finish_reason = "tool_calls"
        elif status == "completed":
            draft.finish_reason = "tool_calls" if draft.calls else "stop"
        elif status == "incomplete":
            draft.finish_reason = "length"
        else:
            raise CutShort(f"unexpected interaction status ({status})")
        return True
    if event_type == "error":
        err = event.get("error")
        code, message = error_body_fields(err)
        if message is None and isinstance(err, str):
            message = err
        raise StreamFailure(code, message)
    return False


# Google native grounded search via the Interactions API.


def _nonneg_int(value):
    return value if type(value) is int and value >= 0 else None


def _add_query(queries, value):
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned and cleaned not in queries:
            queries.append(cleaned)


def _add_source(sources, url, title="", snippet=""):
    if not is_http_url(url):
        return
    clean_title = title.strip() if isinstance(title, str) else ""
    clean_snippet = snippet.strip() if isinstance(snippet, str) else ""
    existing = sources.get(url)
    if existing is None:
        sources[url] = Source(url, clean_title, clean_snippet)
    elif (not existing.title and clean_title) or (
        not existing.snippet and clean_snippet
    ):
        sources[url] = Source(
            url,
            existing.title or clean_title,
            existing.snippet or clean_snippet,
            existing.date,
            existing.site,
            existing.summary,
        )


def _extract_grounding(meta, sources, citations, queries):
    """Extract sources, citation spans, queries, and rendered HTML from grounding."""
    rendered = ""
    if not isinstance(meta, Mapping):
        return rendered
    for raw_query in (
        meta.get("web_search_queries") or meta.get("webSearchQueries") or ()
    ):
        _add_query(queries, raw_query)
    entry_point = meta.get("search_entry_point") or meta.get("searchEntryPoint")
    if isinstance(entry_point, Mapping):
        html = (
            entry_point.get("rendered_content")
            or entry_point.get("renderedContent")
        )
        if isinstance(html, str) and html.strip():
            rendered = html
    chunk_urls = []
    chunks = meta.get("grounding_chunks") or meta.get("groundingChunks") or ()
    for chunk in chunks:
        url = None
        if isinstance(chunk, Mapping):
            web = chunk.get("web") if isinstance(chunk.get("web"), Mapping) else chunk
            candidate = web.get("uri") or web.get("url")
            if is_http_url(candidate):
                url = candidate
                _add_source(sources, url, web.get("title", ""))
        chunk_urls.append(url)
    supports = meta.get("grounding_supports") or meta.get("groundingSupports") or ()
    for support in supports:
        if not isinstance(support, Mapping):
            continue
        segment = support.get("segment")
        if not isinstance(segment, Mapping):
            continue
        text = segment.get("text")
        if not isinstance(text, str) or not text:
            continue
        indices = (
            support.get("grounding_chunk_indices")
            or support.get("groundingChunkIndices")
            or ()
        )
        urls = []
        for idx in indices:
            if type(idx) is int and 0 <= idx < len(chunk_urls):
                url = chunk_urls[idx]
                if url and url not in urls:
                    urls.append(url)
        if not urls:
            continue
        start = _nonneg_int(
            segment.get("start_index", segment.get("startIndex")),
        )
        end = _nonneg_int(segment.get("end_index", segment.get("endIndex")))
        citations.append(CitationSpan(text, tuple(urls), start, end))
    return rendered


def _parse_google_search_response(answer, count):
    """Parse Google Interactions / grounding search JSON into SearchResponse."""
    if not isinstance(answer, Mapping):
        raise SearchError("The response was not a valid search object.")
    status = answer.get("status")
    if status in ("failed", "cancelled"):
        raise SearchError(f"The search response did not complete ({status}).")
    sources = {}
    summary_parts = []
    citations = []
    queries = []
    rendered = ""
    saw_search = False

    outputs = answer.get("outputs") or answer.get("steps") or ()
    for item in outputs:
        if not isinstance(item, Mapping):
            continue
        kind = item.get("type")
        if kind == "google_search_call":
            saw_search = True
            args = item.get("arguments")
            if isinstance(args, Mapping):
                for raw_query in args.get("queries") or ():
                    _add_query(queries, raw_query)
        elif kind == "google_search_result":
            saw_search = True
            raw_results = item.get("result")
            rows = (
                raw_results if isinstance(raw_results, list)
                else [raw_results] if isinstance(raw_results, Mapping)
                else ()
            )
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                url = row.get("url") or row.get("uri")
                _add_source(sources, url, row.get("title", ""), row.get("snippet", ""))
                html = (
                    row.get("rendered_content")
                    or row.get("renderedContent")
                    or row.get("search_suggestions")
                    or row.get("searchSuggestions")
                )
                if isinstance(html, str) and html.strip() and not rendered:
                    rendered = html
        elif kind in ("text", "model_output") or isinstance(item.get("content"), list):
            parts = []
            if isinstance(item.get("text"), str):
                parts.append(item)
            raw_content = item.get("content")
            if isinstance(raw_content, list):
                parts.extend(p for p in raw_content if isinstance(p, Mapping))
            for part in parts:
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    summary_parts.append(text.strip())
                for ann in part.get("annotations") or ():
                    if not isinstance(ann, Mapping):
                        continue
                    url = ann.get("url") or ann.get("uri")
                    if not is_http_url(url):
                        continue
                    _add_source(sources, url, ann.get("title", ""))
                    start = _nonneg_int(
                        ann.get("start_index", ann.get("startIndex")),
                    )
                    end = _nonneg_int(ann.get("end_index", ann.get("endIndex")))
                    span_text = (
                        ann.get("text")
                        if isinstance(ann.get("text"), str) else ""
                    )
                    if (
                        not span_text and isinstance(text, str)
                        and start is not None and end is not None
                        and 0 <= start < end <= len(text)
                    ):
                        span_text = text[start:end]
                    if span_text:
                        citations.append(CitationSpan(span_text, (url,), start, end))
        item_meta = item.get("grounding_metadata") or item.get("groundingMetadata")
        if isinstance(item_meta, Mapping):
            saw_search = True
            found_html = _extract_grounding(item_meta, sources, citations, queries)
            if found_html and not rendered:
                rendered = found_html

    top_meta = answer.get("grounding_metadata") or answer.get("groundingMetadata")
    if isinstance(top_meta, Mapping):
        saw_search = True
        found_html = _extract_grounding(top_meta, sources, citations, queries)
        if found_html and not rendered:
            rendered = found_html

    for candidate in answer.get("candidates") or ():
        if not isinstance(candidate, Mapping):
            continue
        content = candidate.get("content")
        if isinstance(content, Mapping):
            for part in content.get("parts") or ():
                if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                    piece = part["text"].strip()
                    if piece:
                        summary_parts.append(piece)
        cand_meta = (
            candidate.get("grounding_metadata")
            or candidate.get("groundingMetadata")
        )
        if isinstance(cand_meta, Mapping):
            saw_search = True
            found_html = _extract_grounding(cand_meta, sources, citations, queries)
            if found_html and not rendered:
                rendered = found_html

    if not saw_search and not sources and not queries:
        raise SearchError("The response did not include a completed search.")

    selected = list(sources.values())[:count]
    summary = "\n\n".join(summary_parts).strip() if (selected or summary_parts) else ""
    suggestions = (
        SearchSuggestions(tuple(queries), rendered)
        if (queries or rendered) else None
    )
    return SearchResponse(
        selected,
        summary=summary,
        billed=bool(selected or queries),
        citations=tuple(citations),
        suggestions=suggestions,
    )


class GoogleSearch:
    """One isolated stateless Google Interactions request with native google_search."""

    NAME = "google_search"
    MAX_RESULTS = 50
    REQUIRES_MODEL = True

    @staticmethod
    def endpoint(base_url):
        return base_url.rstrip("/") + "/interactions"

    @classmethod
    def search(cls, service, query, count, seconds, transport, check):
        body = {
            "model": service.model,
            "input": json.dumps(
                {"query": query, "count": count}, ensure_ascii=False,
            ),
            "system_instruction": (
                "Search the web for the query. Return up to the requested count "
                "of relevant sources."
            ),
            "tools": [{"type": "google_search"}],
            "generation_config": {"max_output_tokens": 512},
            "store": False,
        }
        headers = (
            {"x-goog-api-key": service.key}
            if service.key and service.key != NO_KEY else {}
        )
        answer = post_json(
            service.url, body, headers, seconds, transport, check, key=service.key,
        )
        return _parse_google_search_response(answer, count)
