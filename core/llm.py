"""One streamed call to a Chat Completions server, local Ollama or an API.

Every call streams, the agent's, the judge's and the consent check's,
through `complete`: one clock on the model's silence, a stop that returns at
once, and each chunk handed on as it comes (the CLI's draft). Which model, at
which server, with which key, is the model component's (model/).
"""

import queue
import re
import threading
import time
from types import SimpleNamespace

from openai import APIError


def usage(response):
    """(prompt, cached, completion) tokens; cached is 0 where not reported."""
    u = response.usage
    cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", None)
    if cached is None:
        # DeepSeek's own name for it
        cached = (u.model_extra or {}).get("prompt_cache_hit_tokens")
    return u.prompt_tokens, cached or 0, u.completion_tokens or 0


# Replies the server cut short (DeepSeek): retried like a dropped connection,
# never taken as the answer, and their tool calls never run
CUT_SHORT = ("insufficient_system_resource", "aborted")


class CutShort(Exception):
    """The server cut the reply short; a retry can get a whole one."""


class Garbled(CutShort):
    """The reply's text carried the provider's tool-call markup: a
    call that went to the text, often only its closing half, so nothing can
    be run from it; retried like a reply cut short."""


# DeepSeek's tool-call markup (DSML) as a tag: `<｜DSML｜invoke …>`, its
# closing `</｜｜DSML｜｜ calls>`, with one or two bars, ASCII or full width
# (OpenClaw's grammar, packages/ai/src/transports/deepseek-dsml-grammar.ts)
MARKUP = re.compile(r"</?(?:\|{1,2}|｜{1,2})DSML(?:\|{1,2}|｜{1,2})")


def garbled(text):
    """True when `text` holds the provider's tool-call markup."""
    return bool(text) and MARKUP.search(text) is not None


class NoReply(Exception):
    """The model sent nothing for its timeout, by the clock, before its first
    token or between two; a retry may get a reply."""


# What a model call can raise that the loop hands to the model_error hook
MODEL_ERRORS = (APIError, CutShort, NoReply)


class Draft:
    """A reply as its chunks come in: the text, the reasoning and the tool
    calls so far, then its finish reason and usage."""

    def __init__(self):
        self.content = ""
        self.reasoning = ""
        # A tool call's index → {"id", "name", "arguments"}, the arguments as
        # far as they have come
        self.calls = {}
        self.finish_reason = None
        self.usage = None

    def add(self, chunk):
        """Fold one chunk in; True if it carried anything (a keep-alive or an
        empty delta doesn't, and so never resets the silence clock)."""
        heard = False
        if getattr(chunk, "usage", None) is not None:
            self.usage = chunk
            heard = True
        for choice in chunk.choices or []:
            delta = choice.delta
            if delta is not None:
                if delta.content:
                    self.content += delta.content
                    heard = True
                extra = delta.model_extra or {}
                reasoning = extra.get("reasoning_content") or extra.get("reasoning")
                if reasoning:
                    self.reasoning += reasoning
                    heard = True
                for piece in delta.tool_calls or []:
                    call = self.calls.setdefault(
                        piece.index, {"id": None, "name": "", "arguments": ""}
                    )
                    if piece.id:
                        call["id"] = piece.id
                    if piece.function is not None:
                        call["name"] += piece.function.name or ""
                        call["arguments"] += piece.function.arguments or ""
                    heard = True
            if choice.finish_reason is not None:
                self.finish_reason = choice.finish_reason
                heard = True
        return heard

    def message(self):
        """The reply as the API takes it back: reasoning_content kept, which
        DeepSeek needs in a tool-call loop; no empty fields."""
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
        if draft.usage is not None:
            self.prompt_tokens, self.cached_tokens, self.completion_tokens = usage(
                draft.usage
            )
        else:
            # A server that sends no usage when streamed: nothing to count
            self.prompt_tokens = self.cached_tokens = self.completion_tokens = 0


def complete(client, model, messages, *, silence, check=None, on_delta=None,
             **options):
    """One streamed model call: its Reply.

    `silence`: seconds the model may send nothing, before its first token and
    between two; past that, NoReply. The SDK's own timeout counts per read, so a
    server that sends a keep-alive now and then never trips it; this clock
    counts only chunks that carry something. `check()`: called every 0.1 s, a
    stop's way in (core/control.py); what it raises ends the call at once, the
    stream closed and anything after dropped. `on_delta(draft)`: after each
    chunk that carried something. Raises CutShort for a stream that ends without
    a finish reason or that the server cut short (Garbled for a text holding the
    model's tool-call markup), and the API's errors as they come (MODEL_ERRORS).
    """
    chunks = queue.Queue()
    streams = []
    closed = threading.Event()

    def read():
        try:
            stream = client.chat.completions.create(
                model=model, messages=messages, stream=True,
                stream_options={"include_usage": True}, **options,
            )
            streams.append(stream)
            if closed.is_set():
                stream.close()
                return
            for chunk in stream:
                if closed.is_set():
                    return
                chunks.put(("chunk", chunk))
            chunks.put(("end", None))
        except BaseException as error:
            # After a close the reader's error is the close's, and nobody waits
            if not closed.is_set():
                chunks.put(("error", error))

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
                kind, item = chunks.get(timeout=min(0.1, left))
            except queue.Empty:
                continue
            if kind == "error":
                raise item
            if kind == "end":
                break
            if draft.add(item):
                heard = time.monotonic()
                if on_delta is not None:
                    on_delta(draft)
    finally:
        closed.set()
        for stream in streams:
            try:
                stream.close()
            except Exception:
                pass
    if draft.finish_reason is None:
        raise CutShort("the stream ended without a finish reason")
    if draft.finish_reason in CUT_SHORT:
        raise CutShort(f"the server cut the reply short ({draft.finish_reason})")
    if garbled(draft.content):
        raise Garbled("the reply's text carried the model's tool-call markup")
    return Reply(draft, time.monotonic() - started)
