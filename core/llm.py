"""One streamed call to a model server, in whichever API the server speaks.

Every call streams, the agent's, the judge's and the consent check's,
through `complete`: one clock on the model's silence, a stop that returns at
once, and each event handed on as it comes (the CLI's draft). The API is
given, as the model component has it (model/apis/: each API's request, and
how its events fold into the draft); which model, at which server, with
which key, is the model component's too.
"""

import queue
import threading
import time
from types import SimpleNamespace

from openai import APIError


class CutShort(Exception):
    """The reply didn't complete: the stream ended without its end, or the
    API said it failed or stopped for a reason other than the output limit.
    Retried like a dropped connection; never taken as the answer, and its
    tool calls never run."""


class NoReply(Exception):
    """The model sent nothing for its timeout, by the clock, before its first
    token or between two; a retry may get a reply."""


# What a model call can raise that the loop hands to the model_error hook
MODEL_ERRORS = (APIError, CutShort, NoReply)


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


class Reply:
    """One model reply, reduced to what the loop needs, from its stream."""

    def __init__(self, draft, seconds=0.0):
        self.message = draft.message()
        self.content = draft.content or None
        self.reasoning = draft.reasoning or None
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
