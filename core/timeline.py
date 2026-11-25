"""Typed log records, branches and the state each branch leaves behind."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from core import attribution, entries, tools
from core.attribution import is_turn_input
from core.entries import Entry

LOG_FORMAT = 8
CORE_SWITCHABLE = ("mode", "model", "workspace")
MODEL_VISIBLE = list(CORE_SWITCHABLE)
SWITCHABLE = list(CORE_SWITCHABLE)
OLD_NAMES = {}
NONE_DEFAULTS = {}
OLD_LOG_FIELDS = {"plugin_errors": "hook_errors"}


def add_switchable(name, aliases=(), *, model_visible=False, none_default=None):
    """Register a component-owned switchable session setting."""
    if name not in SWITCHABLE:
        SWITCHABLE.append(name)
    if model_visible and name not in MODEL_VISIBLE:
        MODEL_VISIBLE.append(name)
    for alias in aliases:
        OLD_NAMES[alias] = name
    if none_default is not None:
        NONE_DEFAULTS[name] = none_default
REWOUND = (
    "[The user rewound this conversation. Continue from here. Files, memory "
    "and running work retain their current state.{effects}]"
)
# Older logs embedded excerpts and call counts in rewind and fork notes.
OLD_BRANCH_EXCERPTS = "\nThe user's messages there:"


@dataclass(frozen=True, slots=True)
class LogRecord:
    seq: int
    entry: Entry
    parent: int | None = None
    log_format: int | None = None

    @property
    def time(self) -> str | None:
        return self.entry.created_at


class OldLog(ValueError):
    """A writer needs to upgrade the log before it can use these records."""

    def __init__(self, found):
        self.found = found
        super().__init__("This session needs a log upgrade before it can run")


class NewerLog(ValueError):
    """This ZERO cannot open a log written in a newer format."""

    def __init__(self, found):
        self.found = found
        super().__init__(
            f"this session's log was written by a newer ZERO (its format "
            f"{found}; this one reads {LOG_FORMAT}): update ZERO to open it"
        )


def marked_format(line, *, first=False):
    """The outer version marker, checked before reading the entry envelope."""
    if not isinstance(line, dict):
        raise ValueError("a log line needs a record object")
    found = line.get("format")
    if (first or line.get("type") == "session") and type(found) is int and found > 1:
        if found > LOG_FORMAT:
            raise NewerLog(found)
        return found
    return None


def native_format(line, *, first=False):
    """Check that a raw line belongs to the native reader before decoding it."""
    found = marked_format(line, first=first)
    if found == LOG_FORMAT and "entry" not in line:
        raise ValueError(f"a format {LOG_FORMAT} log line needs an entry")
    if (found is not None and found < LOG_FORMAT) or "entry" not in line:
        raise OldLog(found or 1)
    return found


def read_record(line, *, first=None) -> LogRecord:
    """Decode one native record. Legacy lines belong to the upgrader."""
    if first is None:
        first = isinstance(line, dict) and line.get("seq") == 1
    found = native_format(line, first=first)
    seq = line.get("seq")
    parent = line.get("parent")
    if type(seq) is not int or seq < 1:
        raise ValueError("a log record needs a positive sequence number")
    if parent is not None and (type(parent) is not int or not 0 <= parent < seq):
        raise ValueError("a log record's parent must precede it")
    entry = Entry.from_dict(line["entry"])
    return LogRecord(seq, entry, parent, found)


def log_format(records: Sequence[LogRecord]):
    """The session header's format. A log with no header needs format 1 recovery."""
    if records and entries.has(records[0].entry, "header"):
        return records[0].log_format or LOG_FORMAT
    return 1


def renamed(values):
    """A log's settings or switch under today's names."""
    result = {}
    for name, value in values.items():
        canonical = OLD_NAMES.get(name, name)
        if value is None and canonical in NONE_DEFAULTS:
            value = NONE_DEFAULTS[canonical]
        result[canonical] = value
    return result


def is_switch(record: LogRecord):
    entry = record.entry
    return entries.has(entry, "switch") or (
        entries.has(entry, "note") and entry.get("kind") == "switch"
    )


def starting_settings(header: LogRecord):
    """The switchable settings recorded on the first header."""
    entry = header.entry
    settings = renamed(entry.get("settings") or {})
    now = {
        "mode": entry.get("mode"),
        "model": entry.get("model"),
        "workspace": settings.get("workspace"),
    }
    for name in SWITCHABLE:
        if name not in now and name in settings:
            now[name] = settings[name]
    return now


def running_with(records: Sequence[LogRecord]):
    """The active switchable settings after the branch's switches."""
    if not records or not entries.has(records[0].entry, "header"):
        return {}
    now = starting_settings(records[0])
    for record in records:
        if is_switch(record):
            switched = renamed(record.entry.get("settings") or {})
            for name in SWITCHABLE:
                if name in switched and name not in CORE_SWITCHABLE:
                    now[name] = switched[name]
            now.update(renamed(record.entry.get("to") or {}))
    return now


def settings_now(records: Sequence[LogRecord]):
    """The settings on the last switch or the first header."""
    for record in reversed(records):
        if is_switch(record):
            return renamed(record.entry.get("settings") or {})
    if records and entries.has(records[0].entry, "header"):
        return renamed(records[0].entry.get("settings") or {})
    return {}


def compact_line(window, compact_at):
    chosen = compact_at or window
    return min(chosen, window) if chosen and window else chosen


def metered(meter, record: LogRecord, compact_at=None):
    """Fold a record's window, model usage, or compacted size into the meter."""
    meter = dict(meter or {"tokens": None, "line": None, "cached_share": None})
    entry = record.entry
    if entries.has(entry, "header") or is_switch(record):
        window = (entry.get("settings") or {}).get("context_limit")
        meter["line"] = compact_line(window, compact_at)
    elif entries.has(entry, "assistant_reply") and (
        prompt := entry.get("prompt_tokens")
    ):
        cached = entry.get("cached_tokens")
        meter["tokens"] = prompt + (entry.get("completion_tokens") or 0)
        meter["cached_share"] = None if cached is None else cached / prompt
    elif entries.has(entry, "context_boundary") and entry.get("tokens_after"):
        meter["tokens"] = entry.get("tokens_after")
        meter["cached_share"] = None
    return meter


def context_meter(records: Sequence[LogRecord], compact_at=None):
    """The branch's context size and cache share after the first model call."""
    meter = None
    for record in records:
        meter = metered(meter, record, compact_at)
    return meter if meter and meter["tokens"] else None


def parent_of(record: LogRecord):
    """The explicit parent at a rewind, or the previous sequence number."""
    return record.seq - 1 if record.parent is None else record.parent


def branch_from(record, by_seq):
    """Follow the parent chain from a record and return it in timeline order."""
    path = []
    while record is not None:
        path.append(record)
        parent = parent_of(record)
        if parent >= record.seq:
            raise ValueError("a log record's parent must precede it")
        record = by_seq.get(parent)
    path.reverse()
    return path


def branch(records: Sequence[LogRecord]):
    """The current branch from the root to the last record."""
    if not any(record.parent is not None for record in records):
        return records
    return branch_from(records[-1], {record.seq: record for record in records})


def is_rewind_point(record: LogRecord):
    """Whether this user message began a turn that can be rewound."""
    entry = record.entry
    return (entries.has(entry, "user_message") and not entry.get("steer")
            and not entry.get("continue") and not attribution.is_notice(entry))


def rewind_points(records: Sequence[LogRecord]):
    """The user's turn-opening messages on the current branch, oldest first."""
    return [record for record in branch(records) if is_rewind_point(record)]


def left_behind_by(records: Sequence[LogRecord], seq):
    """The record before a rewind point and the branch that the rewind drops."""
    path = branch(records)
    for index, record in enumerate(path):
        if record.seq == seq:
            if not is_rewind_point(record):
                raise ValueError(f"record {seq} isn't one of the user's messages "
                                 "that starts a turn")
            return path[index - 1], path[index:]
    raise ValueError(f"record {seq} isn't on the conversation's current branch")


def branch_note(entry: Entry):
    """Render a branch note without repeating the discarded conversation."""
    text = entries.text(entry)
    if OLD_BRANCH_EXCERPTS not in text:
        return text
    if entries.has(entry, "rewind"):
        return REWOUND.format(effects="")
    return "[This conversation forked. The workspace, files and memory are shared.]"


def without_question_echoes(records: Sequence[LogRecord]):
    """Older question replies also logged the prompt and choice as a user steer."""
    answered = set()
    for record in records:
        entry = record.entry
        inbox = entry.get("inbox")
        if entries.has(entry, "answer") and entry.get("kind") == "question" and inbox:
            answered.add(inbox)
        if is_turn_input(entry) and entry.get("steer") and inbox in answered:
            continue
        yield record


def turns(records: Sequence[LogRecord]):
    """Turns in the trajectory's shape, with a restarted turn kept together."""
    result = []
    steps = {}
    for record in without_question_echoes(records):
        entry = record.entry
        number = entry.get("step")
        if is_turn_input(entry):
            text = attribution.delivered_text(entry)
            if entry.get("steer") and result:
                result[-1].setdefault("steers", []).append(text)
                continue
            result.append({"question": text, "steps": []})
            steps = {}
        elif not result:
            continue
        elif entries.has(entry, "assistant_reply"):
            step = {
                "step": number, "content": entries.project(entry) or None,
                "tool_calls": [],
                **{name: entry.get(name) for name in (
                    "finish_reason", "prompt_tokens", "cached_tokens",
                    "completion_tokens", "retries", "model_seconds",
                )},
                "seconds": 0.0,
            }
            if entry.get("empty"):
                step["empty"] = True
            if "reply" in (entry.fields or {}):
                step["reply"] = entries.thaw(entry.get("reply"))
            steps[number] = step
            result[-1]["steps"].append(step)
        elif entries.has(entry, "tool_result") and entry.get("closed") != "not_started":
            steps[number]["tool_calls"].append({
                **{name: entry.get(name) for name in (
                    "tool", "arguments", "result", "bad_call", "denied",
                )},
                **entries.thaw(entry.get("notes") or {}),
            })
        elif entries.has(entry, "step") and number in steps:
            step = steps[number]
            step["seconds"] = entry.get("seconds")
            errors = entry.get("hook_errors") or entry.get("plugin_errors")
            if errors:
                step["hook_errors"] = entries.thaw(errors)
        elif entries.has(entry, "turn_end"):
            result[-1].update({
                OLD_LOG_FIELDS.get(name, name): entries.thaw(entry.get(name))
                for name in (
                    "outcome", "seconds", "agent_seconds", "wait_seconds",
                    "detail", "answer", "reply", "error", "hook_errors",
                    "plugin_errors",
                )
                if name in (entry.fields or {})
            })
    return result


def open_turn_start(start, index, record: LogRecord):
    """Update the start of the unfinished turn when a record starts or closes it."""
    if is_turn_input(record.entry) and not record.entry.get("steer"):
        return index
    if entries.has(record.entry, "turn_end"):
        return None
    return start


def unfinished_turn(records: Sequence[LogRecord]):
    """The unfinished turn's records, or an empty slice."""
    start = None
    for index, record in enumerate(records):
        start = open_turn_start(start, index, record)
    return records[start:] if start is not None else records[:0]


def parked_ask(records: Sequence[LogRecord], *, current_turn=False):
    """The ask still waiting for a reply in the unfinished turn."""
    ask = None
    for record in records if current_turn else unfinished_turn(records):
        entry = record.entry
        if entries.has(entry, "ask"):
            ask = record
        elif entries.has(entry, "answer") and ask is not None:
            if entries.linked_to(entry, "answers") == ask.entry.id:
                ask = None
    return ask


def lost_reply(records: Sequence[LogRecord], *, current_turn=False):
    """The legacy reply a restart must still write into the conversation."""
    answer = None
    for record in records if current_turn else unfinished_turn(records):
        entry = record.entry
        if entries.has(entry, "answer"):
            cancelled = (not entry.get("yes") and entry.get("kind") != "question"
                         and "interaction" not in (entry.fields or {})
                         and entry.get("choice") != "no")
            answer = record if cancelled else None
        elif answer is not None and is_turn_input(entry):
            if entry.get("inbox") == answer.entry.get("inbox"):
                answer = None
    return answer


def dangling_calls(records: Sequence[LogRecord]):
    """Calls without a result, paired with the assistant reply's step."""
    pending = {}
    for record in records:
        entry = record.entry
        if entries.has(entry, "assistant_reply"):
            for call in entries.tool_calls(entry):
                pending[call.id or entries.call_id(call)] = (entry.get("step"), call)
        elif entries.has(entry, "tool_result"):
            target = entries.linked_to(entry, "answers")
            if pending.pop(target, None) is None:
                for key, (_, call) in tuple(pending.items()):
                    if entries.call_id(call) == target:
                        del pending[key]
    return list(pending.values())


def closing_results(records: Sequence[LogRecord], result, closed):
    """Result fields for unresolved calls, including saved question replies."""
    asks = {
        entry.id: (entry.get("step"), entries.linked_to(entry, "asks_about"))
        for record in records
        if entries.has(entry := record.entry, "ask") and entry.get("kind") == "question"
    }
    answers = {
        asks[target]: attribution.question_result(entry, entry.get("text"))
        for record in records if entries.has(entry := record.entry, "answer")
        and entry.get("kind") == "question"
        and (target := entries.linked_to(entry, "answers")) in asks
    }
    for step, raw in dangling_calls(records):
        name = raw.get("name")
        arguments = raw.get("arguments")
        call_id = entries.call_id(raw)
        through = {}
        if inner := tools.unwrapped(name, arguments):
            through = {"called_through": name}
            name, arguments = inner
        _, error = tools.check(name, arguments)
        fields = {
            "step": step, "call": call_id, "tool": name, "arguments": arguments,
            "result": result, "bad_call": bool(error), "denied": False, "ran": False,
            "notes": {}, "closed": closed, **through,
        }
        if (step, call_id) in answers:
            fields.update(result=answers[step, call_id], ran=True)
            del fields["closed"]
        yield fields


def restart_note(records: Sequence[LogRecord], now=None, extra=""):
    """Describe the gap and any observed side effects before repair writes a record."""
    last = datetime.fromisoformat(records[-1].time)
    now = now or datetime.now().astimezone()
    seconds = max(0, int((now - last).total_seconds()))
    text = (
        f"This turn was cut off {span(seconds)} ago, when ZERO stopped; "
        "it has just restarted. Nothing has come from the user since."
    )
    return f"[{text}{extra}]"


def span(seconds):
    """Format a gap for the model in seconds, minutes, hours or days."""
    for size, unit, below in (
        (1, "second", 60), (60, "minute", 120 * 60), (3600, "hour", 48 * 3600),
    ):
        if seconds < below:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'}"
    count = seconds // 86400
    return f"{count} day{'' if count == 1 else 's'}"


def ended_fresh(records: Sequence[LogRecord]):
    """The answer when a fresh compaction ended a turn before it could close."""
    for at in range(len(records) - 1, -1, -1):
        entry = records[at].entry
        if entries.has(entry, "context_boundary") and entry.get("fresh"):
            last = next((row.entry for row in reversed(records[:at])
                         if entries.has(row.entry, "assistant_reply")), None)
            return (entries.project(last) or "").strip() if last is not None else ""
        if is_turn_input(entry) or any(entries.has(entry, trait) for trait in (
            "assistant_reply", "tool_result",
        )):
            return None
    return None


def final_answer(records: Sequence[LogRecord]):
    """The latest nonempty reply without further tool calls in this turn."""
    for record in reversed(records):
        entry = record.entry
        if is_turn_input(entry):
            return None
        if entries.has(entry, "assistant_reply"):
            if not entry.get("empty") and entry.get("finish_reason") != "length":
                text = entries.project(entry)
                if not any(entries.tool_calls(entry)) and text.strip():
                    return text
            return None
    return None
