"""OpenAI's Chat Completions API (`POST /v1/chat/completions`), the one
nearly every model server speaks: Ollama, llama-server, vLLM, and the
OpenAI-shaped doors of the big providers.

ZERO's messages are kept in this API's shape (core/llm.py, Draft.message),
so they go as they are. Its stream is chunks: deltas of text, reasoning and
tool calls, a finish reason, then the usage (asked for with
stream_options). Reasoning isn't in the API's own reference; servers that
show it send `reasoning_content` (llama-server, DeepSeek) or `reasoning`
(Ollama, vLLM, OpenRouter), and both are read.
"""

from core.llm import CutShort

# Its name in a server's entry (the line `api`), and in words
NAME = "chat_completions"
TITLE = "the Chat Completions API"

# What ZERO may ask of a model through this API's standard fields
# (features.py): thinking off and on (reasoning_effort), strict JSON
# (response_format json_schema) and JSON mode (response_format json_object)
FEATURES = ("thinking_off", "thinking_on", "strict_json", "json_mode")
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


def request(model, messages, *, tools=None, effort=None, json_schema=None,
            json_mode=False, temperature=None, max_output_tokens=None):
    """The request's fields. `json_schema`: {"name", "schema"}, strict JSON;
    `json_mode`: any JSON object."""
    fields = {
        "model": model, "messages": messages, "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        fields["tools"] = tools
    if effort is not None:
        fields["reasoning_effort"] = EFFORTS[effort]
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
    return fields


def open_stream(client, model, messages, **standard):
    """The streamed request, on the SDK's client."""
    return client.chat.completions.create(
        **request(model, messages, **standard)
    )


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
            for piece in delta.tool_calls or []:
                call = draft.call(piece.index)
                if piece.id:
                    call["id"] = piece.id
                if piece.function is not None:
                    call["name"] += piece.function.name or ""
                    call["arguments"] += piece.function.arguments or ""
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
