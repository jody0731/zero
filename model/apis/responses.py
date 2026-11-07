"""OpenAI's Responses API (`POST /v1/responses`), the one OpenAI, DeepSeek,
xAI and Codex have moved to.

ZERO keeps no state on the server (`store` false): every request carries the
whole conversation, as typed items. ZERO's messages (the Chat Completions
API's shape, core/llm.py) become them here: the first system message the
request's `instructions`; a message each turn of text; an assistant turn's
thinking a `reasoning` item of plain text, its calls `function_call` items,
and each result a `function_call_output`, paired by `call_id`. Its stream is
typed events: deltas of text, reasoning and a call's arguments, each output
item whole when it is done, then the response completed, incomplete (the
output limit, or another reason) or failed, with its usage.
"""

from core.llm import CutShort

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


def items(messages):
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
            if message.get("reasoning_content"):
                found.append({
                    "type": "reasoning", "summary": [],
                    "content": [{"type": "reasoning_text",
                                 "text": message["reasoning_content"]}],
                })
            if message.get("content"):
                found.append({"type": "message", "role": "assistant",
                              "content": message["content"]})
            for call in message.get("tool_calls", ()):
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
            json_mode=False, temperature=None, max_output_tokens=None):
    """The request's fields. `json_schema`: {"name", "schema"}, strict JSON;
    `json_mode`: any JSON object."""
    instructions, found = items(messages)
    fields = {"model": model, "input": found, "stream": True, "store": False}
    if instructions is not None:
        fields["instructions"] = instructions
    if tools:
        fields["tools"] = [tool(schema) for schema in tools]
    if effort is not None:
        fields["reasoning"] = {"effort": EFFORTS[effort]}
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
    """The streamed request, on the SDK's client."""
    return client.responses.create(**request(model, messages, **standard))


def usage(draft, response):
    used = getattr(response, "usage", None)
    if used is None:
        return
    details = getattr(used, "input_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) or 0
    draft.tokens = (used.input_tokens or 0, cached, used.output_tokens or 0)


def fold(draft, event):
    """One event into the draft; True if it carried anything (the response
    made, a part begun: nothing, so the silence clock runs on). Raises
    CutShort for a response that failed, or that is incomplete for a reason
    other than the output limit or a content filter."""
    kind = getattr(event, "type", None)
    if kind == "response.output_text.delta":
        draft.content += event.delta
        return bool(event.delta)
    if kind == "response.reasoning_text.delta":
        draft.reasoning += event.delta
        return bool(event.delta)
    if kind in ("response.output_item.added", "response.output_item.done"):
        item = event.item
        if getattr(item, "type", None) != "function_call":
            return False
        call = draft.call(event.output_index)
        call["id"] = item.call_id
        call["name"] = item.name
        # Whole when done; while it streams, the deltas build it
        if kind == "response.output_item.done" or item.arguments:
            call["arguments"] = item.arguments or ""
        return True
    if kind == "response.function_call_arguments.delta":
        draft.call(event.output_index)["arguments"] += event.delta
        return bool(event.delta)
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
        said = getattr(error, "message", None) or "no reason given"
        raise CutShort(f"the response failed: {said}")
    if kind == "error":
        raise CutShort(f"the response failed: {event.message}")
    return False
