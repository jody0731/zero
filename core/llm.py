"""One streamed call to a model server, in whichever API the server speaks.

Every call streams, the agent's, the judge's and the consent check's,
through `complete`: one clock on the model's silence, a stop that returns at
once, and each event handed on as it comes (the CLI's draft). The API is
given, as the model component has it (model/apis/: each API's request, and
how its events fold into the draft); which model, at which server, with
which key, is the model component's too.
"""

import json
import queue
import re
import threading
import time
import unicodedata
from types import SimpleNamespace
from urllib.parse import quote, quote_plus

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError


class InvalidHistory(ValueError):
    """A model request would split a call from its result."""


def check_tool_results(messages):
    pending = set()
    for message in messages:
        if message["role"] == "tool":
            call = message.get("tool_call_id")
            if call not in pending:
                raise InvalidHistory(f"tool result has no matching call: {call}")
            pending.remove(call)
        else:
            if pending:
                missing = ", ".join(sorted(pending))
                raise InvalidHistory(f"missing tool results: {missing}")
            for call in message.get("tool_calls") or ():
                key = call.get("id")
                if not isinstance(key, str) or not key or key in pending:
                    raise InvalidHistory("tool calls need distinct nonempty ids")
                pending.add(key)
    if pending:
        raise InvalidHistory(f"missing tool results: {', '.join(sorted(pending))}")


class CutShort(Exception):
    """The reply didn't complete: the stream ended without its end, or the
    API said it failed or stopped for a reason other than the output limit.
    Retried like a dropped connection; never taken as the answer, and its
    tool calls never run."""


class NoReply(Exception):
    """The model sent nothing for its timeout, by the clock, before its first
    token or between two; a retry may get a reply."""


class UnsupportedModelRequest(Exception):
    """The selected model and API cannot satisfy this request as configured."""


class StreamFailure(CutShort):
    """A provider error event that ends the stream."""

    def __init__(self, code, message):
        super().__init__(f"the response failed: {message or 'no reason given'}")
        self.code = code
        self.message = message


# What a model call can raise that the loop hands to the model_error hook
MODEL_ERRORS = (APIError, CutShort, NoReply, UnsupportedModelRequest)


def safe_error_text(text, key=None, redact=None):
    """Remove credentials and control characters before clipping a detail."""
    if not isinstance(text, str):
        return ""
    if isinstance(key, str) and key:
        forms = {key, quote(key, safe=""), quote_plus(key),
                 json.dumps(key, ensure_ascii=False)[1:-1]}
        pattern = "|".join(map(re.escape, sorted(forms, key=len, reverse=True)))
        text = re.sub(pattern, "[redacted]", text)
    if redact is not None:
        text = redact(text)
    text = "".join(" " if unicodedata.category(char).startswith("C") else char
                   for char in text)
    text = " ".join(text.split())
    return text if len(text) <= 600 else text[:599].rstrip() + "…"


def request_failure(error, operation, server, *, title=None, key=None, redact=None):
    """The facts a failed model request can tell an app, safe to write to a log."""
    return {"operation": operation, "server": server, "server_title": title or server,
            **request_error_details(error, key=key, redact=redact)}


def request_error_details(error, *, key=None, redact=None):
    """The provider's reported fields and what the harness observed."""
    report = {}
    code = message = observation = None
    if isinstance(error, APIStatusError):
        report["http_status"] = error.status_code
    if isinstance(error, StreamFailure):
        code, message = error.code, error.message
    elif isinstance(error, APITimeoutError):
        observation = "ZERO reached its request timeout."
    elif isinstance(error, APIConnectionError):
        observation = "Connection error."
    elif isinstance(error, (NoReply, CutShort, UnsupportedModelRequest)):
        observation = str(error)
    elif isinstance(error, APIError):
        # The SDK unwraps `error` in HTTP and stream errors. Only use named
        # fields, never the SDK's exception text or a raw HTTP response.
        body = error.body
        if isinstance(body, dict):
            code, message = body.get("code"), body.get("message")
        elif isinstance(body, str):
            if isinstance(error, APIStatusError):
                # An explicit JSON error string can be used. A raw body cannot.
                try:
                    payload = error.response.json()
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    message = payload.get("error")
            else:
                message = body
    if type(code) is int:
        code = str(code)
    for field, text in (("provider_code", code), ("provider_message", message),
                        ("observation", observation)):
        if cleaned := safe_error_text(text, key, redact):
            report[field] = cleaned
    if not report:
        report["observation"] = "The model request ended with an error."
    return report


def failure_text(report, *, provider_details=True):
    """A short string for callers that read the older text fields."""
    status = report.get("http_status")
    parts = [f"HTTP {status}"] if status is not None else []
    if provider_details:
        parts.extend(report[field] for field in ("provider_code", "provider_message")
                     if field in report)
    if report.get("observation"):
        parts.append(report["observation"])
    return " · ".join(parts) or "The provider reported a stream error."


def request_error_text(error, client):
    """Safe diagnostic text for a caller that records a string."""
    key = getattr(client, "api_key", None)
    if isinstance(error, MODEL_ERRORS):
        text = failure_text(request_error_details(error, key=key))
    else:
        text = safe_error_text(str(error), key)
    return f"{type(error).__name__}: {text}"


class Draft:
    """A reply as its events come in: the text, the reasoning and the tool
    calls so far, then how it ended and its tokens. An API's `fold` fills it
    (model/apis/), in ZERO's words whatever the API's own: `finish_reason`
    is "stop", "tool_calls", "length" (the output limit) or
    "content_filter"."""

    def __init__(self):
        self.content = ""
        self.reasoning = ""
        # A tool call's index → {"id", "name", "arguments"}, the arguments as
        # far as they have come
        self.calls = {}
        # Provider-specific continuation data, private to the producing route.
        self.response_items = {}
        self.thought_signatures = {}
        self.reasoning_details = {}
        self.finish_reason = None
        # Prompt tokens, of those the cache's, and output tokens; None until
        # the API says
        self.tokens = None

    def call(self, index):
        """The tool call at `index`, made empty when it is new."""
        return self.calls.setdefault(
            index, {"id": None, "name": "", "arguments": ""}
        )

    def message(self):
        """The reply as ZERO keeps it, in the Chat Completions API's shape
        (each API sends it in its own): reasoning_content kept, for an API
        that takes a turn's thinking back; no empty fields."""
        message = {"role": "assistant"}
        if self.content:
            message["content"] = self.content
        if self.reasoning:
            message["reasoning_content"] = self.reasoning
        if self.calls:
            message["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"], "arguments": call["arguments"]},
                }
                for _, call in sorted(self.calls.items())
            ]
        return message


def without_provider_state(message):
    """A model message without private continuation data."""
    if "provider_state" not in message:
        return message
    return {key: value for key, value in message.items() if key != "provider_state"}


def provider_history(messages, route):
    """Keep continuation data only for the endpoint, API and model that made it."""
    found = messages
    for index, message in enumerate(messages):
        state = message.get("provider_state")
        if state and state.get("route") != route:
            if found is messages:
                found = list(messages)
            found[index] = {key: value for key, value in message.items()
                            if key not in ("provider_state", "reasoning_content")}
    return found


def remember_provider_reply(reply, route, *, response_items=True):
    """Store route-bound continuation on the assistant's local history."""
    items = reply.response_items if response_items else []
    if items or reply.thought_signatures or reply.reasoning_details or reply.reasoning:
        reply.message["provider_state"] = {
            "route": route, "items": items,
            "thought_signatures": reply.thought_signatures,
            "reasoning_details": reply.reasoning_details,
        }


class Reply:
    """One model reply, reduced to what the loop needs, from its stream."""

    def __init__(self, draft, seconds=0.0):
        self.message = draft.message()
        self.content = draft.content or None
        self.reasoning = draft.reasoning or None
        self.response_items = [
            item for _, item in sorted(draft.response_items.items())
        ]
        self.thought_signatures = {
            draft.calls[index]["id"]: {
                "name": draft.calls[index]["name"],
                "arguments": draft.calls[index]["arguments"],
                "extra_content": signature,
            }
            for index, signature in draft.thought_signatures.items()
            if index in draft.calls
        }
        self.reasoning_details = [
            item for _, item in sorted(draft.reasoning_details.items())
        ]
        self.tool_calls = [
            SimpleNamespace(
                id=call["id"],
                function=SimpleNamespace(
                    name=call["name"], arguments=call["arguments"]
                ),
            )
            for _, call in sorted(draft.calls.items())
        ]
        self.finish_reason = draft.finish_reason
        self.seconds = seconds
        # A server that sends no usage when streamed: nothing to count
        self.prompt_tokens, self.cached_tokens, self.completion_tokens = (
            draft.tokens or (0, 0, 0)
        )


def complete(api, client, model, messages, *, silence, check=None,
             on_delta=None, **request):
    """One streamed model call: its Reply.

    `api`: the API the server speaks (model/apis/), which opens the stream on
    `client` from the messages and the request's standard fields (`tools`,
    `effort`, `json_schema`, `json_mode`, `temperature`,
    `max_output_tokens`) and folds each event into the draft. `silence`:
    seconds the model may send nothing, before its first token and between
    two; past that, NoReply. The SDK's own timeout counts per read, so a
    server that sends a keep-alive now and then never trips it; this clock
    counts only events that carry something, or a server's progress through
    the prompt (the API's `alive`). `check()`: called every 0.1 s, a
    stop's way in (core/control.py); what it raises ends the call at once, the
    stream closed and anything after dropped. `on_delta(draft)`: after each
    event that carried something. Raises CutShort for a reply that didn't
    complete, and the API's errors as they come (MODEL_ERRORS).
    """
    check_tool_results(messages)
    events = queue.Queue()
    streams = []
    closed = threading.Event()

    def read():
        try:
            stream = api.open_stream(client, model, messages, **request)
            streams.append(stream)
            if closed.is_set():
                stream.close()
                return
            for event in stream:
                if closed.is_set():
                    return
                events.put(("event", event))
            events.put(("end", None))
        except BaseException as error:
            # After a close the reader's error is the close's, and nobody waits
            if not closed.is_set():
                events.put(("error", error))

    threading.Thread(target=read, daemon=True, name="model-stream").start()
    draft = Draft()
    started = heard = time.monotonic()
    try:
        while True:
            if check is not None:
                check()
            left = heard + silence - time.monotonic()
            if left <= 0:
                if heard == started:
                    raise NoReply(f"no reply from the model in {silence:g} s")
                raise NoReply(f"the model went silent for {silence:g} s")
            try:
                kind, item = events.get(timeout=min(0.1, left))
            except queue.Empty:
                continue
            if kind == "error":
                raise item
            if kind == "end":
                break
            if api.fold(draft, item):
                heard = time.monotonic()
                if on_delta is not None:
                    on_delta(draft)
            elif getattr(api, "alive", None) and api.alive(item):
                # The server is reading the prompt: alive, nothing to show
                heard = time.monotonic()
    finally:
        closed.set()
        for stream in streams:
            try:
                stream.close()
            except Exception:
                pass
    if draft.finish_reason is None:
        raise CutShort("the stream ended before the reply did")
    return Reply(draft, time.monotonic() - started)
