"""OpenAI's Chat Completions API (`POST /v1/chat/completions`), the one
nearly every model server speaks: Ollama, llama-server, vLLM, and the
OpenAI-shaped doors of the big servers.

ZERO's messages are kept in this API's shape (core/llm.py, Draft.message),
so they go as JSON through the SDK's public post method, without a second
walk through its request types. Its stream is chunks: deltas of text, reasoning and
tool calls, a finish reason, then the usage (asked for with
stream_options). Reasoning isn't in the API's own reference; servers that
show it send `reasoning_content` (llama-server, DeepSeek) or `reasoning`
(Ollama, vLLM, OpenRouter), and both are read.
"""

from openai import Stream
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from core.llm import CutShort, without_provider_state

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


def with_continuation(message):
    """Give a matching Chat endpoint back the continuation data it issued."""
    state = message.get("provider_state")
    if state is None:
        return message
    found = without_provider_state(message)
    signatures = state.get("thought_signatures") or {}
    if signatures and found.get("tool_calls"):
        calls = []
        for call in found["tool_calls"]:
            signature = signatures.get(call["id"], {})
            function = call["function"]
            if (signature.get("name") == function["name"]
                    and signature.get("arguments") == function["arguments"]):
                call = {**call, "extra_content": signature["extra_content"]}
            calls.append(call)
        found["tool_calls"] = calls
    if details := state.get("reasoning_details"):
        found["reasoning_details"] = details
        found.pop("reasoning_content", None)
    return found


def request(model, messages, *, tools=None, effort=None, json_schema=None,
            json_mode=False, temperature=None, max_output_tokens=None,
            prefill_progress=False, thinking_field=None):
    """The request's fields. `json_schema`: {"name", "schema"}, strict JSON;
    `json_mode`: any JSON object; `prefill_progress`: llama.cpp's progress
    chunks while it reads the prompt."""
    wire = messages
    if any("provider_state" in message for message in messages):
        wire = [with_continuation(message) for message in messages]
    fields = {
        "model": model, "messages": wire, "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        fields["tools"] = tools
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


def open_stream(client, model, messages, **standard):
    """Send prepared JSON; the SDK handles HTTP and parses streamed chunks."""
    body = request(model, messages, **standard)
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
    for value in details:
        if not isinstance(value, dict):
            continue
        index = value.get("index")
        if type(index) is not int:
            index = len(draft.reasoning_details)
        item = draft.reasoning_details.setdefault(index, {})
        for name, data in value.items():
            if data is None and name in item:
                continue
            if (name in ("text", "summary", "data", "signature")
                    and isinstance(data, str) and isinstance(item.get(name), str)):
                item[name] += data
            else:
                item[name] = data


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
        heard = True
    for choice in chunk.choices or []:
        delta = choice.delta
        if delta is not None:
            if delta.content:
                draft.content += delta.content
                heard = True
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
                call = draft.call(piece.index)
                if piece.id:
                    call["id"] = piece.id
                if piece.function is not None:
                    call["name"] += piece.function.name or ""
                    call["arguments"] += piece.function.arguments or ""
                extra_content = (piece.model_extra or {}).get("extra_content")
                google = (extra_content.get("google")
                          if isinstance(extra_content, dict) else None)
                if isinstance(google, dict) and google.get("thought_signature"):
                    draft.thought_signatures[piece.index] = {
                        "google": {"thought_signature": google["thought_signature"]},
                    }
                heard = True
        if choice.finish_reason is not None:
            reason = FINISHED.get(choice.finish_reason)
            if reason is None:
                raise CutShort(
                    f"the reply didn't complete ({choice.finish_reason})"
                )
            draft.finish_reason = reason
            heard = True
    return heard
