"""The context kept under its line, as hooks.

Before every model call, mid-turn too, the context's size plus the
headroom is checked against the line, and a compaction comes first when it
doesn't fit; ZERO's `compact` call and the user's request do the same at the
next call, or at once when the session is idle (`compact`), always with a
handoff, and say so plainly when there is nothing to hand off. A server that
still refuses a request as too long gets one compaction and the call again:
a net, never the plan. Each result and notice ZERO didn't size itself gets a
share of the room left, never less than its share of the room a compaction
leaves (near the line one comes before the next step anyway, and keeps the
step whole); past it, the whole goes to the session's results folder, and
ZERO reads why, how big, where, and its start and end. A compaction ZERO or
the user asks for may carry their words on what its handoff keeps.
"""

import re
import time
from datetime import datetime
from uuid import uuid4

from components.context import compaction, entries as context_entries, results, room
from components.context.compaction import (
    INSTRUCTION_TOKENS, CompactionFailed, NothingToCompact,
)
from core import entries, replay, tools
from core.hooks import Hook, Retry, Stop
from core.llm import MODEL_ERRORS, failure_text

# Error phrases and codes for a request that exceeds the server's context.
TOO_LONG = re.compile(
    r"context.{0,40}(length|size|window)|exceeds?.{0,40}context|too long|"
    r"context_length_exceeded|exceed_context_size",
    re.IGNORECASE | re.DOTALL,
)
SAVED = (
    "[This result is about {cost:,} tokens, more than its share of this "
    "session's context ({share}). The whole of it is in {path} ({lines:,} "
    "lines){reading}.{preview}]"
)
# How to read a saved result, by the tool that reads a file in parts and its
# two arguments (tools carrying "reads_in_parts" in their "context" metadata)
READING = (": `{tool}` with {start} and {count} reads any part of it, or all "
           "of it at once")
SHARE = "{share:,} tokens: {percent}% of the {left:,} left before it is compacted"
# Near the line: a share of the room a compaction leaves, never less (it
# comes before the next step there, and keeps that step whole)
SHARE_AFTER = "{share:,} tokens: {percent}% of the {room:,} left after a compaction"
COMES_NEXT = ", which comes before the next step"
NO_ROOM = "none: the context is compacted before the next step"
PREVIEW = " Its start and end follow."
BETWEEN = "\n[… {count:,} lines not shown …]\n"
# A sized result bigger than a fresh context can hold: the one wall left
TOO_BIG = (
    "Not given whole: this result is about {cost:,} tokens, more than a fresh "
    "context holds below this session's line ({most:,} tokens).{parts}"
)
PARTS = " It can be read in parts with {start} and {count}."
# Shorter than this, a preview isn't worth its room
LEAST_PREVIEW_TOKENS = 40


def bytes_a_token(session, *, records=None):
    """The bytes a token `session`'s text is estimated at: the session's own
    (room.bytes_a_token) where its model server refuses a request longer
    than its context, so a guess low is caught by the refusal; else, and
    before a step has counted, one (never low). The memory reads the
    server's facts, never its name."""
    server = getattr(session.model, "server", None)
    if not getattr(server, "refuses_overlong_requests", False):
        return room.BYTES_PER_TOKEN
    if records is None:
        records = session.log.branch()
    return room.bytes_a_token(records) or room.BYTES_PER_TOKEN


def counter(session, *, records=None):
    """Text's tokens for `session`: its model server's count where it gives
    one, else its bytes over the session's bytes a token (room.py)."""
    count = getattr(session.model, "count_tokens", None)
    ratio = bytes_a_token(session, records=records)

    def tokens(text):
        found = count(text) if count else None
        return room.tokens(text, ratio) if found is None else found

    return tokens


def counted(records):
    """Whether a model call has been made since the context's latest
    compaction, and counted it where its server reports a count. Until one
    has, the line isn't checked: a compaction leaves a context estimated
    whole, a token a byte where the server counts nothing and may cut an
    overlong request (four times high for English), so the next check read
    it past the line and compacted again at once (7.3's first eval). The
    next call goes ahead, its count decides, and a refusal still meets the
    net."""
    for record in reversed(records):
        if entries.has(record.entry, "assistant_reply"):
            return True
        if entries.has(record.entry, "compaction_handoff"):
            return False
    return True


def too_long(error):
    """Whether a model call's error is the server refusing a request as longer
    than its context."""
    code = getattr(error, "status_code", None)
    return code in (400, 413) and bool(TOO_LONG.search(str(error)))


def preview(text, tokens, bytes_a_token=room.BYTES_PER_TOKEN):
    """`text`'s start and end in about `tokens` tokens at `bytes_a_token`,
    cut at lines where it can be; "" when there is no room for one."""
    if tokens < LEAST_PREVIEW_TOKENS:
        return ""
    data = text.encode("utf-8")
    side = int(tokens * bytes_a_token) // 2
    head = data[:side].decode("utf-8", errors="ignore")
    tail = data[-side:].decode("utf-8", errors="ignore")
    if "\n" in head:
        head = head[:head.rindex("\n")]
    if "\n" in tail:
        tail = tail[tail.index("\n") + 1:]
    shown = results.lines_of(head) + results.lines_of(tail)
    hidden = max(results.lines_of(text) - shown, 0)
    return f"\n\n{head}{BETWEEN.format(count=hidden)}{tail}"


def _part_reader(context):
    """Return (tool_name, start_arg, count_arg) for a tool that reads files in parts."""
    for name, meta in tools.tools_with_metadata("context", context):
        if reader := meta.get("reads_in_parts"):
            return (name, *reader)
    return None


class Context(Hook):
    """The context of one session kept under its line. `window`: the model's
    context window; `folder`: the session's results folder (a callable, as a
    new log is named when its first turn starts); `limits`: the session's,
    read at each check (the line, the share, the reply's and the handoff's
    room)."""

    def __init__(self, window, folder, limits):
        self.window = window
        self.folder = folder
        self.limits = limits
        # A turn whose automatic compaction failed doesn't try again, so a
        # server that can't write a handoff isn't asked at every step
        self._failed = False
        self._blocked = None
        self._refused = set()
        self._compaction_cursor = (0, None)
        # An empty string is a tool request without handoff instructions.
        self._tool_compaction_instructions = None
        self._wanted = None

    def _want_compaction(self, by, instructions=None):
        """Record a compaction request to be honored before the next model call."""
        self._wanted = (by, instructions)

    def _take_wanted(self):
        """Return and clear any pending direct compaction request."""
        wanted, self._wanted = self._wanted, None
        return wanted

    def _line(self, session):
        """The line under the window of the model the session runs on now: a
        turn gone on with the fallback model (core/loop.py) has its window."""
        server = getattr(session.model, "server", None)
        window = getattr(server, "context_window", None) or self.window
        return room.line(self.limits, window)

    def _size(self, session, records=None):
        if records is None:
            records = session.log.branch()
        return room.measure(records, session.safe_entries(), session.tool_schemas,
                            counter(session, records=records))

    def _fixed(self, session):
        """The tokens every context holds: the system message and the tools
        (room.fixed_tokens)."""
        records = session.log.branch()
        return room.fixed_tokens(records, session.safe_entries(),
                                 session.tool_schemas(),
                                 counter(session, records=records))

    def _room_after(self, session, at):
        """The room a handoff leaves below the line `at`: a fresh context's,
        less the steps it keeps (a share of the line, never less than the
        newest step, which holds the result being fitted)."""
        count = counter(session)
        body = replay.in_context(session.log.branch())
        points = compaction.cut_points(body)
        newest = body[points[-1]:] if points else body
        by_id = {entry.id: entry for entry in session.safe_entries()}
        step = sum(room.entry_tokens(by_id[entry.id], count) for record in newest
                   if (entry := replay.model_entry(record)) is not None
                   and entry.id in by_id)
        kept = max(int(at * compaction.TAIL_SHARE), step)
        return room.fresh_room(at, self._fixed(session), INSTRUCTION_TOKENS,
                               self.limits, **room.model_limits(session.model)) - kept

    def turn_start(self, turn):
        self._failed = False
        self._blocked = None
        self._refused = set()

    def before_model(self, step):
        """A compaction first when the context and its headroom pass the line,
        or when ZERO or the user asked for one."""
        session = step.turn.session
        try:
            self._check_model_window(session)
        except room.ModelLimitExceeded as error:
            return Stop("failed", str(error))
        records = session.log.branch()
        reason, instructions = self._asked(session, records)
        if reason is None and not self._failed:
            has_limits = room.has_input_bound(session.model)
            is_counted = counted(records)
            if has_limits or is_counted:
                size = self._size(session, records)
                left = room.room_left(self._line(session), size,
                                      INSTRUCTION_TOKENS, self.limits,
                                      **room.model_limits(session.model))
                if left < 0 and is_counted:
                    reason = "line"
                elif has_limits:
                    try:
                        room.bound_output(session.model, size.tokens,
                                          operation="The context")
                    except room.ModelLimitExceeded:
                        reason = "line"
        if reason is not None:
            self._compact(session, reason, step.turn.control, instructions)
        if self._blocked:
            return Stop("failed", self._blocked)
        return None

    def before_model_request(self, step, conversation, schemas):
        """Check the prepared input and reserve only as much output as fits."""
        session = step.turn.session
        server = getattr(session.model, "server", None)
        if not room.has_input_bound(session.model):
            step.max_output_tokens = getattr(server, "max_output_tokens", None)
            return None
        # Use the provider's last count for the conversation it already saw.
        records = session.log.branch()
        size = room.measure(records, conversation, schemas,
                            counter(session, records=records))
        try:
            step.max_output_tokens = room.bound_output(
                session.model, size.tokens, operation="The prepared reply input",
            )
        except room.ModelLimitExceeded as error:
            return Stop("failed", str(error))
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

    def compaction_asked(self, session, reason, handoff=None, fresh=False,
                         occasion=None, instructions=None):
        """A compaction now, between turns (the user's request, with their
        words on what its handoff keeps: `instructions`), or one that keeps
        nothing but `handoff` (`fresh`: a round's end, rounds/, on its
        `occasion`); one that fails is written down as `compaction_failed`."""
        # Consume any pending mid-turn request so it doesn't fire twice later.
        take = session.tool_context.get("take_wanted_compaction")
        if take is not None:
            take()
        self._take_wanted()
        try:
            self._check_model_window(session)
        except room.ModelLimitExceeded as error:
            self._compaction_failed(session, reason, error)
            return
        if not fresh:
            words = {reason: instructions} if instructions else None
            self._compact(session, reason, instructions=words)
            return
        try:
            compaction.fresh(session, handoff, self._line(session),
                             counter(session), limits=self.limits,
                             reason=reason, occasion=occasion)
        except (CompactionFailed, room.ModelLimitExceeded, *MODEL_ERRORS) as error:
            self._compaction_failed(session, reason, error)

    def _check_model_window(self, session):
        server = getattr(session.model, "server", None)
        refresh = getattr(server, "refresh_limits", None)
        if refresh is not None:
            refresh()
            if server.context_window is None:
                raise room.ModelLimitExceeded(
                    f"ZERO no longer has a context window for {session.model.name}. "
                    "Choose the model again to set its window."
                )

    def _compaction_failed(self, session, reason, error):
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        if isinstance(error, MODEL_ERRORS):
            report = session.model_request_failure(error, "compact_context")
            fields = {
                "reason": reason,
                "error": f"{type(error).__name__}: {failure_text(report)}",
                "model_failure": {**report, "next_action": "stop"},
            }
        else:
            fields = {
                "reason": reason,
                "error": f"{type(error).__name__}: {error}",
            }
        entry = entries.make(
            "compaction_failed", id=uuid4().hex, origin="system",
            created_at=stamp, fields=fields,
        )
        session.write(entry)

    def _asked(self, session, branch=None):
        """(who asked for a compaction since the last one: "user" through the
        server, "zero" with its tool, else None; the askers' words on what
        its handoff keeps, {"user" or "zero": their words}). The user's
        request names the compaction where both asked; ZERO's words stay."""
        reason, words = None, {}
        take = session.tool_context.get("take_wanted_compaction")
        wanted = take() if take is not None else None
        if not wanted:
            wanted = self._take_wanted()
        if wanted:
            reason, text = wanted
            if text:
                key = reason if reason in context_entries.ASKERS else "user"
                words[key] = text
        if branch is None:
            branch = session.log.branch()
        offset, tail = self._compaction_cursor
        # The same record at the same offset identifies the saved branch prefix.
        if offset and (offset > len(branch) or branch[offset - 1] is not tail):
            offset = 0
            self._tool_compaction_instructions = None
        for record in reversed(branch[offset:]):
            entry = record.entry
            if (replay.starts_context(entry) or entries.has(entry, "compaction_failed")
                    or entries.has(entry, "nothing_to_compact")):
                self._tool_compaction_instructions = None
                break
            if entries.has(entry, "tool_result"):
                notes = entry.get("notes") or {}
                if notes.get("compact"):
                    self._tool_compaction_instructions = (
                        notes.get("compact_instructions") or ""
                    )
                    break
        self._compaction_cursor = (len(branch), branch[-1] if branch else None)
        if self._tool_compaction_instructions is not None:
            reason = reason or "zero"
            if self._tool_compaction_instructions:
                words["zero"] = self._tool_compaction_instructions
        return reason, words

    def _compact(self, session, reason, control=None, instructions=None):
        """The compaction record, or None when it failed (written down in the
        log as `compaction_failed`). One with nothing older than the newest
        step to hand off isn't a failure: asked for, the log says so
        (`nothing_to_compact`); at the line, right after a compaction the
        whole context is estimated, with its margin, so a big newest step can
        still read as past the line, and the call goes ahead (a server's
        refusal is still caught by the net)."""
        check = control.check if control is not None else None
        try:
            return compaction.compact(session, reason, self._line(session),
                                      self.folder(), check, counter(session),
                                      limits=self.limits,
                                      instructions=instructions)
        except (CompactionFailed, room.ModelLimitExceeded, *MODEL_ERRORS) as error:
            if isinstance(error, NothingToCompact) and reason != "refused":
                if reason in context_entries.ASKERS:
                    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
                    entry = entries.make(
                        "nothing_to_compact", id=uuid4().hex, origin="system",
                        created_at=stamp, fields={"reason": reason},
                    )
                    session.write(entry)
                return None
            self._failed = True
            if isinstance(error, room.ModelLimitExceeded):
                self._blocked = str(error)
            self._compaction_failed(session, reason, error)
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
            most = room.fresh_room(at, self._fixed(session), INSTRUCTION_TOKENS,
                                   self.limits, **room.model_limits(session.model))
            if cost > most:
                reader = _part_reader(session.tool_context)
                parts = PARTS.format(start=reader[1], count=reader[2]) if (
                    reader) else ""
                return TOO_BIG.format(cost=cost, most=most, parts=parts)
            return None
        name = f"{call.name}-{call.id}.txt"
        return self._fit(session, at, result, cost, name, call.notes.get("whole_at"),
                         call)

    def notice(self, text, session):
        """A notice (a job's end, with its output) fitted as a result is."""
        name = f"notice-{time.strftime('%Y%m%d-%H%M%S')}-{time.monotonic_ns()}.txt"
        return self._fit(session, self._line(session), text,
                         counter(session)(text), name)

    def _fit(self, session, at, text, cost, name, whole_at=None, call=None):
        """`text` as the context takes it: None when it fits its share, the
        share of the room left or, near the line, of the room a compaction
        leaves, whichever is more (the share shrank to none before: 6.3);
        past it, saved whole and ZERO told so, the file's path set on
        `call` (the tool call's record) where one is given."""
        left = room.room_left(at, self._size(session), INSTRUCTION_TOKENS,
                              self.limits, **room.model_limits(session.model))
        after = self._room_after(session, at)
        allowed = room.share(max(left, after), self.limits)
        if cost <= allowed:
            return None
        path = whole_at or results.save(self.folder(), name, text)
        if call is not None:
            # As data beside the text, for an app that links it
            call.saved_path = path
        shown = preview(text, allowed, bytes_a_token(session))
        percent = self.limits["result_share_percent"]
        if after > left and allowed > 0:
            told = SHARE_AFTER.format(share=allowed, percent=percent, room=after)
            # What it shows of the result fills the room left, so the
            # context passes its line
            told += COMES_NEXT if left < allowed else ""
        elif left > 0:
            told = SHARE.format(share=allowed, percent=percent, left=left)
        else:
            told = NO_ROOM
        reader = _part_reader(session.tool_context)
        reading = READING.format(tool=reader[0], start=reader[1],
                                 count=reader[2]) if reader else ""
        return SAVED.format(
            cost=cost, share=told, path=path, lines=results.lines_of(text),
            reading=reading, preview=PREVIEW if shown else "",
        ) + shown
