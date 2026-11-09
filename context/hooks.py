"""The context kept under its line, as hooks.

Before every model call, mid-turn too, the context's size plus the
headroom is checked against the line, and a compaction comes first when it
doesn't fit; ZERO's `compact` call and the user's request do the same at the
next call, or at once when the session is idle (`compact`). A server that
still refuses a request as too long gets one compaction and the call again:
a net, never the plan. Each result and notice ZERO didn't size itself gets a
share of the room left; past it, the whole goes to the session's results
folder, and ZERO reads why, how big, where, and its start and end.
"""

import re
import time

from context import compaction, results, room
from context.compaction import (
    INSTRUCTION_TOKENS, CompactionFailed, NothingToCompact,
)
from core.hooks import Hook, Retry
from core.llm import MODEL_ERRORS

# What a server says when a request is longer than its context: DeepSeek's
# and OpenAI's ("maximum context length"), llama-server's ("exceeds the
# available context size"), OpenAI's code
TOO_LONG = re.compile(
    r"context.{0,40}(length|size|window)|exceeds?.{0,40}context|too long|"
    r"context_length_exceeded|exceed_context_size",
    re.IGNORECASE | re.DOTALL,
)
SAVED = (
    "[This result is about {cost:,} tokens, more than its share of the room "
    "left before this session's context is compacted ({share}). The whole of "
    "it is in {path} ({lines:,} lines): `read` with offset and limit reads any "
    "part of it, or all of it at once.{preview}]"
)
SHARE = "{share:,} tokens: 15% of the {left:,} left"
NO_ROOM = "none: the context is compacted before the next step"
PREVIEW = " Its start and end follow."
BETWEEN = "\n[… {count:,} lines not shown …]\n"
# A sized result bigger than a fresh context can hold: the one wall left
TOO_BIG = (
    "Not given whole: this result is about {cost:,} tokens, more than a fresh "
    "context holds below this session's line ({most:,} tokens). It can be read "
    "in parts with offset and limit."
)
# Shorter than this, a preview isn't worth its room
LEAST_PREVIEW_TOKENS = 40


def counter(session):
    """Text's tokens for `session`: its model server's count where it gives
    one, else a token a byte (room.py)."""
    count = getattr(session.model, "count_tokens", None)

    def tokens(text):
        found = count(text) if count else None
        return room.tokens(text) if found is None else found

    return tokens


def too_long(error):
    """Whether a model call's error is the server refusing a request as longer
    than its context."""
    code = getattr(error, "status_code", None)
    return code in (400, 413) and bool(TOO_LONG.search(str(error)))


def preview(text, tokens):
    """`text`'s start and end in about `tokens` tokens, cut at lines where it
    can be; "" when there is no room for one."""
    if tokens < LEAST_PREVIEW_TOKENS:
        return ""
    data = text.encode("utf-8")
    side = tokens * room.BYTES_PER_TOKEN // 2
    head = data[:side].decode("utf-8", errors="ignore")
    tail = data[-side:].decode("utf-8", errors="ignore")
    if "\n" in head:
        head = head[:head.rindex("\n")]
    if "\n" in tail:
        tail = tail[tail.index("\n") + 1:]
    shown = results.lines_of(head) + results.lines_of(tail)
    hidden = max(results.lines_of(text) - shown, 0)
    return f"\n\n{head}{BETWEEN.format(count=hidden)}{tail}"


class Context(Hook):
    """The context of one session kept under its line. `window`: the model's
    context window; `folder`: the session's results folder (a callable, as a
    new log is named when its first turn starts)."""

    def __init__(self, window, folder):
        self.window = window
        self.folder = folder
        # A turn whose automatic compaction failed doesn't try again, so a
        # server that can't write a handoff isn't asked at every step
        self._failed = False
        self._refused = set()

    def _line(self, session):
        """The line under the window of the model the session runs on now: a
        turn gone on with the fallback model (core/loop.py) has its window."""
        server = getattr(session.model, "server", None)
        window = getattr(server, "context_window", None) or self.window
        return room.line(session.settings, window)

    def _size(self, session):
        return room.measure(session.log.branch(), session.messages,
                            session.tool_schemas(), counter(session))

    def turn_start(self, turn):
        self._failed = False
        self._refused = set()

    def before_model(self, step):
        """A compaction first when the context and its headroom pass the line,
        or when ZERO or the user asked for one."""
        session = step.turn.session
        reason = self._asked(session)
        if reason is None and not self._failed:
            left = room.room_left(self._line(session), self._size(session),
                                  INSTRUCTION_TOKENS)
            if left < 0:
                reason = "line"
        if reason is None:
            return None
        self._compact(session, reason, step.turn.control)
        return None

    def model_error(self, failure):
        """The server refused a request as too long: compact, and try again
        once for this step."""
        if not too_long(failure.error) or failure.step.number in self._refused:
            return None
        self._refused.add(failure.step.number)
        session = failure.step.turn.session
        if not self._compact(session, "refused", failure.step.turn.control):
            return None
        return Retry(0)

    def compaction_asked(self, session, reason, handoff=None):
        """A compaction now, between turns (the user's request), or at a
        round's end, keeping nothing but `handoff` (rounds/); one that fails
        is written down as `compaction_failed`."""
        if reason != "round":
            self._compact(session, reason)
            return
        try:
            compaction.fresh(session, handoff, self._line(session),
                             counter(session))
        except (CompactionFailed, *MODEL_ERRORS) as error:
            session.write("compaction_failed", reason=reason,
                          error=f"{type(error).__name__}: {error}")

    def _asked(self, session):
        """"user" or "zero" when a compaction was asked for since the last
        one, else None."""
        if getattr(session, "compact_asked", None):
            session.compact_asked = None
            return "user"
        for record in reversed(session.log.branch()):
            if record["type"] in ("compaction", "compaction_failed", "system",
                                  "session"):
                return None
            if record["type"] == "tool" and record.get("notes", {}).get("compact"):
                return "zero"
        return None

    def _compact(self, session, reason, control=None):
        """The compaction record, or None when it failed (written down in the
        log as `compaction_failed`). At the line, one that can't make room
        isn't a failure: right after a compaction the whole context is
        estimated, with its margin, so a big newest step can still read as
        past the line, and the call goes ahead (a server's refusal is
        still caught by the net)."""
        check = control.check if control is not None else None
        try:
            return compaction.compact(session, reason, self._line(session),
                                      self.folder(), check, counter(session))
        except (CompactionFailed, *MODEL_ERRORS) as error:
            if reason == "line" and isinstance(error, NothingToCompact):
                return None
            self._failed = True
            session.write("compaction_failed", reason=reason,
                          error=f"{type(error).__name__}: {error}")
            return None

    def tool_result(self, result, call):
        """A result ZERO didn't size, past its share: saved whole, and ZERO
        told why, how big, where, and its start and end. A sized one bigger
        than a fresh context holds is the one wall."""
        session = call.turn.session
        at = self._line(session)
        count = counter(session)
        cost = count(result)
        if call.notes.get("sized"):
            fixed = count(session.messages[0]["content"])
            most = room.fresh_room(at, fixed, INSTRUCTION_TOKENS)
            if cost > most:
                return TOO_BIG.format(cost=cost, most=most)
            return None
        name = f"{call.name}-{call.id}.txt"
        return self._fit(session, at, result, cost, name, call.notes.get("whole_at"))

    def notice(self, text, session):
        """A notice (a job's end, with its output) fitted as a result is."""
        name = f"notice-{time.strftime('%Y%m%d-%H%M%S')}-{time.monotonic_ns()}.txt"
        return self._fit(session, self._line(session), text,
                         counter(session)(text), name)

    def _fit(self, session, at, text, cost, name, whole_at=None):
        left = room.room_left(at, self._size(session), INSTRUCTION_TOKENS)
        allowed = room.share(left)
        if cost <= allowed:
            return None
        path = whole_at or results.save(self.folder(), name, text)
        shown = preview(text, allowed)
        told = SHARE.format(share=allowed, left=left) if left > 0 else NO_ROOM
        return SAVED.format(
            cost=cost, share=told, path=path, lines=results.lines_of(text),
            preview=PREVIEW if shown else "",
        ) + shown
