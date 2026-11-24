"""Google's native Interactions API (`POST /v1beta/interactions`).

ZERO keeps no state on the server (`store` false): every request carries the
full conversation as flat chronological steps (`user_input`, `thought`,
`model_output`, `function_call`, `function_result`) and the leading system
prompt as `system_instruction`. Authentication uses the `x-goog-api-key` header
over `httpx2`, with no provider SDK dependency.
"""

import json
from collections.abc import Mapping

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
from components.model.limits import TokenLimits, model_id, token_count

NAME = "google_interactions"
TITLE = "the Google Interactions API"
FEATURES = ("thinking_off", "thinking_on", "strict_json", "json_mode")
MIN_OUTPUT_TOKENS = 1
RETRY_V1_PREFIX = False
EFFORTS = {"off": "minimal", "low": "low", "high": "high", "max": "xhigh"}

NO_KEY = "none"
MAX_LIST_PAGES = 20
CALL_STEP_FIELDS = frozenset({"type", "id", "name", "arguments", "signature"})
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
    """List native models supporting generation, following nextPageToken."""
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
                methods = item.get("supportedGenerationMethods") or ()
                if "generateContent" not in methods:
                    continue
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
                found.append({
                    "id": name,
                    "context_window": limits.context_window,
                    **limits.as_dict(),
                })
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


def input_steps(conversation, *, route=None, project=None):
    """System instruction and flat chronological input steps from the timeline."""
    system_instruction = None
    found = []
    call_names = {}
    projections = (project or project_entries)(conversation)
    timeline = tool_timeline(conversation, lambda node: projections[id(node)].tools)
    for index, (entry, calls, results, _) in enumerate(timeline):
        projected = projections[id(entry)]
        passages = projected.passages
        if (index == 0 and entry.origin == "system"
                and entries.has(entry, "system_prompt")):
            system_instruction = entries.project(entry)
            if system_instruction:
                passages = passages[1:]
        if projected.assistant or calls:
            for call in calls:
                call_names[entries.call_id(call)] = call.get("name") or ""
            found.extend(assistant_steps(entry, route, calls, projected.content))
        for result, cid in results:
            output = tool_output(result)
            step = {
                "type": "function_result",
                "call_id": cid,
                "name": result.get("tool") or call_names.get(cid, ""),
                "result": "" if output is None else output,
            }
            if result.get("failed") or result.get("denied") or result.get("bad_call"):
                step["is_error"] = True
            found.append(step)
        for role, text in passages:
            step_type = "model_output" if role == "assistant" else "user_input"
            found.append({
                "type": step_type,
                "content": [{"type": "text", "text": text}],
            })
    return system_instruction, found


def tool(schema):
    """Convert a Chat-shaped tool schema to an Interactions function tool."""
    function = schema["function"]
    return {
        "type": "function",
        "name": function["name"],
        "description": function.get("description", ""),
        "parameters": normalize_gemini_schema(function.get("parameters", {})),
    }


def request(model, conversation, *, tools=None, effort=None, json_schema=None,
            json_mode=False, temperature=None, max_output_tokens=None,
            reasoning_summary=False, route=None, project=None):
    """Build the JSON request body for `POST /interactions`."""
    system_instruction, steps = input_steps(
        conversation, route=route, project=project,
    )
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
    body = request(model, conversation, **standard)
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
    draft.unrecognized_outputs[index] = dict(delta)
    return False


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
