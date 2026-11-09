"""Pure views of the session log: model messages and the evals' turn shape.

The log keeps everything, as a tree: a record's parent is the one before
it, unless it names another (a `rewind`'s is the point the user went back
to), and the current branch is the path from the last record back to the
first (`branch`). A log with no rewind is one straight branch, as every log
written before 0.6.6 is. The context, what one model call sends, is a view
of the branch: the latest system message (the session's own, or one rebuilt
since: a `system` record, or a compaction's), then the latest compaction's
handoff and the records it kept, then every record after it (`replay`).
Each user message carries its time at its end, so a gap between two shows.
"""

import re
from datetime import datetime

from core import tools

LOG_FORMAT = 2
UNKNOWN = (
    "Tool call interrupted before a result was recorded; its outcome is unknown."
)
# Before a message the user sent while a turn ran, in the model's copy
MID_TURN = "[The user wrote this while you were working.]"
# At the end of each user message in the model's copy: when it was sent
SENT = "[Sent {when}]"
# The records that carry a system message: the session's first, one rebuilt
# (a model switch, new code) and a compaction's
SYSTEM_RECORDS = ("session", "system", "compaction")
# Around a notice in the model's copy: who sent it, by its source (NOTICES).
# What a notice is, the system message says once (prompt/instructions.py);
# until 0.6.4 each notice said it itself, at a cost in every notice
NOTICE = '<notice from="{source}">\n{text}\n</notice>'
# A closing tag inside a notice (a job's output may hold anything) is
# escaped, so what follows it still reads as the notice's
CLOSING_TAG = re.compile(r"<(/\s*notice)", re.IGNORECASE)
# Before a compaction's handoff, in the model's copy
HANDOFF = (
    "[Compacted {when}: the conversation before this point is replaced by "
    "this handoff, which you wrote. The session's log keeps all of it, and "
    "the outputs cleared from view are saved whole in the files named.]"
)


# Where a compaction happened, as the model reads it: what it did, and how
# big the context is after it, so a notice from before it ("compacted before
# the next step") isn't read as still standing
COMPACTED = "[Compacted {when}: {what}{size}]"
CLEARED_ONLY = ("older outputs were saved whole and cleared from view, each in "
                "the file its line names.")
HANDED_OFF = "the conversation before the kept steps became the handoff above."
# A round's end (rounds/): nothing is kept but the handoff
ROUND_ENDED = ("a round ended, and the conversation before this point became "
               "the handoff above.")
SIZE = " The context is now about {tokens:,} tokens of the {line:,} it is kept under."

# Where the user rewound, as the model reads it: what was left behind and
# what of it still stands; each part that changes things adds its lines
# (the `rewound` hooks), so the note names what the log knows exactly
REWOUND = (
    "[Rewound {when}: the user went back to before their message of "
    "{asked}. The conversation from there, records {first}–{last} of this "
    "session's log{session}, is left behind and is no longer in your "
    "context; the log keeps it, and session_search reads it by those record "
    "numbers. What was done there stays done: files and memory are as it "
    "left them.{part}{extra}]"
)
# The session's id in the rewind's note, where the server gives it
REWOUND_SESSION = " (session {session})"
# What a part of the conversation the context doesn't hold did, in the
# rewind's note (the part left behind) and a fork's (the part its parent
# went on with): the user's messages there, then the calls that ran
PART_MESSAGES = "\nThe user's messages there:\n{messages}"
# One of the user's messages there: its record and its first line, the
# whole in the log
PART_MESSAGE = "- record {seq}: {line}"
# The calls that ran there, counted by tool
PART_CALLS = "\nCalls that ran there, by tool: {counts}."
# Past this, a message's line is cut, the cut saying so
PART_LINE_CHARS = 200
# Inside a notice whose sender started on a part a rewind left behind
LEFT_BEHIND = (
    "[This started from record {seq} of this session's log, on a part of the "
    "conversation a rewind left behind.]"
)


class OldLog(ValueError):
    """The server must upgrade this log before handing it to the harness."""


# What the user may change between a session's turns, each logged as a
# switch: its `to` values and the session's settings from then on. A change
# to what the agent knows of itself (MODEL_VISIBLE) is a `switch` note the
# model reads; the judge's alone is a `switch` record it doesn't
MODEL_VISIBLE = ("mode", "model", "workspace", "thinking_effort")
SWITCHABLE = (*MODEL_VISIBLE, "judge_model", "judge_thinking_effort")
# Two settings a log written before 0.6.2.8 names otherwise
OLD_NAMES = {"effort": "thinking_effort", "judge_effort": "judge_thinking_effort"}
# A step's and a turn's field a log written before 0.6.2.8 names otherwise
OLD_LOG_FIELDS = {"plugin_errors": "hook_errors"}


def renamed(values):
    """A log's settings or switch under today's names."""
    return {OLD_NAMES.get(name, name): value for name, value in values.items()}


def is_switch(record):
    if record["type"] == "switch":
        return True
    return record["type"] == "note" and record.get("kind") == "switch"


def chosen_judge(settings):
    """The judge a session's settings hold, as the user left it: its model
    (None: the session's own) and its effort. A log written before the judge
    was the session's own setting has only the judge it ran with, kept as it
    was; None when it says nothing (no judge in its mode)."""
    if "judge_model" in settings:
        return {"judge_model": settings["judge_model"],
                "judge_thinking_effort": settings.get("judge_thinking_effort")}
    ran = settings.get("judge")
    if ran:
        return {"judge_model": ran.get("model"),
                "judge_thinking_effort": ran.get("effort")}
    return None


def running_with(records):
    """The mode, model, workspace, effort and judge the log runs with: its
    header's, then each switch's; {} before the first turn. The judge's two
    come from the settings it runs with now, and are missing where those say
    nothing of them."""
    if not records or records[0].get("type") != "session":
        return {}
    head = records[0]
    now = {
        "mode": head.get("mode"),
        "model": head.get("model"),
        "workspace": head.get("settings", {}).get("workspace"),
        "thinking_effort": renamed(head.get("settings", {})).get(
            "thinking_effort"
        ),
    }
    for record in records:
        if is_switch(record):
            now.update(renamed(record["to"]))
    now.update(chosen_judge(settings_now(records)) or {})
    return now


def settings_now(records):
    """The settings the session runs with: its header's, or the last switch's."""
    settings = records[0]["settings"]
    for record in records:
        if is_switch(record):
            settings = record["settings"]
    return renamed(settings)


def metered(meter, record):
    """The user's meter of the context after `record`: a session or switch
    record sets the line it is kept under (compact_at_tokens, else the
    model's window, never past it: context/room.py's line); a model call its
    tokens (its prompt and reply) and the share of its prompt the server's
    cache served (None where the server doesn't say); a compaction the tokens
    it left, and no share until the next call."""
    meter = dict(meter or {"tokens": None, "line": None, "cached_share": None})
    kind = record.get("type")
    if kind == "session" or (kind in ("switch", "note") and is_switch(record)):
        settings = renamed(record.get("settings") or {})
        window = settings.get("context_limit")
        chosen = settings.get("compact_at_tokens") or window
        meter["line"] = min(chosen, window) if chosen and window else chosen
    elif kind == "assistant" and record.get("prompt_tokens"):
        prompt = record["prompt_tokens"]
        cached = record.get("cached_tokens")
        meter["tokens"] = prompt + (record.get("completion_tokens") or 0)
        meter["cached_share"] = None if cached is None else cached / prompt
    elif kind == "compaction" and record.get("tokens_after"):
        meter["tokens"] = record["tokens_after"]
        meter["cached_share"] = None
    return meter


def context_meter(records):
    """The meter as a whole log has it (metered, record by record); None
    before the first model call."""
    meter = None
    for record in records:
        meter = metered(meter, record)
    return meter if meter and meter["tokens"] else None


def parent_of(record):
    """A record's parent: the one it names (a rewind's), else the one before."""
    return record.get("parent", record["seq"] - 1)


def branch(records):
    """The current branch: from the first record to the last along their
    parents, in order. A log no rewind has branched is all of its records."""
    if not any("parent" in record for record in records):
        return records
    by_seq = {record["seq"]: record for record in records}
    path = []
    record = records[-1]
    while record is not None:
        path.append(record)
        record = by_seq.get(parent_of(record))
    path.reverse()
    return path


def is_rewind_point(record):
    """Whether the user can rewind to before this record: one of their own
    messages that starts a turn (not a steer, a notice or a `continue`)."""
    return (record["type"] == "user" and not record.get("steer")
            and not record.get("continue") and not is_notice(record))


def rewind_points(records):
    """The user's own messages on the current branch that start a turn,
    oldest first: where a rewind or a fork can begin."""
    return [record for record in branch(records) if is_rewind_point(record)]


def left_behind_by(records, seq):
    """What a rewind to before the user's message `seq` leaves behind: the
    branch from that message on. ValueError if it isn't a rewind point on
    the current branch."""
    path = branch(records)
    for index, record in enumerate(path):
        if record.get("seq") == seq:
            if not is_rewind_point(record):
                raise ValueError(f"record {seq} isn't one of the user's messages "
                                 "that starts a turn")
            return path[index - 1], path[index:]
    raise ValueError(f"record {seq} isn't on the conversation's current branch")


def part_lines(part):
    """What a part of the conversation did, from its records: the user's
    messages there (steers included; not notices or a `continue`), each by
    its record and first line, then the calls that ran there counted by
    tool. The rewind's note says it of the part left behind, a fork's of the
    part its parent went on with."""
    lines = []
    for record in part:
        if (record["type"] != "user" or record.get("continue")
                or is_notice(record)):
            continue
        text = record["text"].strip()
        line = text.splitlines()[0] if text else ""
        if len(line) > PART_LINE_CHARS or line != text:
            line = line[:PART_LINE_CHARS].rstrip() + " … (the whole in the log)"
        lines.append(PART_MESSAGE.format(seq=record["seq"], line=line))
    counts = {}
    for record in part:
        if record["type"] == "tool" and record.get("ran"):
            counts[record["tool"]] = counts.get(record["tool"], 0) + 1
    text = PART_MESSAGES.format(messages="\n".join(lines))
    if counts:
        text += PART_CALLS.format(counts=", ".join(
            f"{name} {count}" for name, count in counts.items()
        ))
    return text


def rewind_note(when_iso, left, extra="", session=None):
    """The rewind's note, from the part it leaves: what was done there
    (`part_lines`), what the hooks add (`extra`: the files written there,
    the jobs started there that still run), and `session`, its id, for
    session_search."""
    return REWOUND.format(
        when=when(when_iso), asked=when(left[0]["time"]),
        first=left[0]["seq"], last=left[-1]["seq"],
        session=REWOUND_SESSION.format(session=session) if session else "",
        part=part_lines(left), extra=extra,
    )


def log_format(records):
    if records and records[0].get("type") == "session":
        if records[0].get("format") == LOG_FORMAT:
            return LOG_FORMAT
    return 1


def when(iso):
    """A record's time as the model reads it: Sat 7 Nov 2026, 21:47 EST."""
    moment = datetime.fromisoformat(iso).astimezone()
    return f"{moment:%a} {moment.day} {moment:%b %Y, %H:%M %Z}"


def sent(record):
    """A user message's text with its time at the end; a notice's in its
    tag, saying so when its sender started on a part a rewind left behind."""
    text = record["text"]
    if is_notice(record):
        if record.get("left_behind"):
            text = f"{LEFT_BEHIND.format(seq=record['started_from_seq'])}\n{text}"
        text = NOTICE.format(source=record["source"],
                             text=CLOSING_TAG.sub(r"<\\\1", text))
    return f"{text}\n\n{SENT.format(when=when(record['time']))}"


def compacted(record):
    """A compaction where it happened: what it did and the size it left."""
    what = CLEARED_ONLY if record.get("cleared_only") else HANDED_OFF
    if record.get("reason") == "round":
        what = ROUND_ENDED
    size = ""
    if record.get("tokens_after") and record.get("line"):
        size = SIZE.format(tokens=record["tokens_after"], line=record["line"])
    return COMPACTED.format(when=when(record["time"]), what=what, size=size)


def message(record):
    """The message the model sees for one record, or None. A record that
    carries a system message after the session's first (a rebuild, a
    compaction) changes the context as a whole: `replay` places it; a
    compaction also says where it happened."""
    kind = record["type"]
    if kind == "session":
        return {"role": "system", "content": record["system_prompt"]}
    if kind == "user" and record.get("mid_turn"):
        return {"role": "user", "content": f"{MID_TURN}\n{sent(record)}"}
    if kind == "user":
        return {"role": "user", "content": sent(record)}
    if kind in ("note", "rewind"):
        return {"role": "user", "content": record["text"]}
    if kind == "compaction":
        return {"role": "user", "content": compacted(record)}
    if kind == "assistant" and not record.get("empty"):
        return record["message"]
    if kind == "tool":
        return {
            "role": "tool",
            "tool_call_id": record["call"],
            "content": record["result"],
        }
    return None


# The sources whose messages are notices from ZERO, not the user's words, each
# with what sends it, as the system message names it: each part that sends
# them declares its own (jobs/tool.py), so a new one (a subagent's report) is
# one more entry
NOTICES = {}


def is_notice(message):
    """Whether a message, a user record or a steer, is a notice."""
    return message.get("source") in NOTICES


def steer_fields(steer):
    """A message a running turn takes in, as its record's fields. The user's
    own are `mid_turn`, so the model reads that they came while it
    worked; a notice says what it is itself."""
    fields = {"text": steer["text"], "steer": True}
    for name in ("inbox", "source", "started_from_seq"):
        if steer.get(name) is not None:
            fields[name] = steer[name]
    if not is_notice(steer):
        fields["mid_turn"] = True
    return fields


def latest_compaction(records):
    """The latest compaction record, or None."""
    for record in reversed(records):
        if record["type"] == "compaction":
            return record
    return None


def system_now(records):
    """The latest record that carries a system message."""
    for record in reversed(records):
        if record["type"] in SYSTEM_RECORDS:
            return record
    return None


def in_context(records):
    """The records the context is made of after its system message and any
    handoff: since the latest compaction, the ones it kept (from its
    `kept_from`) and every one after it, the compactions among them where
    they happened; else all of them."""
    compaction = latest_compaction(records)
    body = records
    if compaction is not None:
        kept = compaction["kept_from"]
        body = [r for r in records if r["seq"] >= kept]
    return [r for r in body if r["type"] not in ("session", "system")]


def handoff(compaction):
    """A compaction's handoff as the model reads it, or None for one that
    only cleared outputs and had none to carry."""
    if not compaction.get("summary"):
        return None
    head = HANDOFF.format(when=when(compaction.get("handoff_at")
                                    or compaction["time"]))
    return {"role": "user", "content": f"{head}\n\n{compaction['summary']}"}


def replay(records):
    """The context: of the log's current branch, the latest system message,
    the latest compaction's handoff, then the messages of the records in it,
    with the outputs that compaction cleared as it left them."""
    if not records:
        return []
    records = branch(records)
    system = system_now(records)
    messages = [{"role": "system", "content": system["system_prompt"]}]
    compaction = latest_compaction(records)
    cleared = {}
    if compaction is not None:
        cleared = compaction.get("cleared") or {}
        start = handoff(compaction)
        if start is not None:
            messages.append(start)
    for record in in_context(records):
        found = message(record)
        if found is None:
            continue
        if record["type"] == "tool" and str(record["seq"]) in cleared:
            found = {**found, "content": cleared[str(record["seq"])]}
        messages.append(found)
    return messages


def turns(records):
    """Turns in the trajectory's shape; a restart keeps the same turn."""
    result = []
    steps = {}
    for record in records:
        kind = record["type"]
        if kind == "user":
            if record.get("steer") and result:
                result[-1].setdefault("steers", []).append(record["text"])
                continue
            result.append({"question": record["text"], "steps": []})
            steps = {}
        elif not result:
            continue
        elif kind == "assistant":
            step = {
                "step": record["step"],
                "content": record["message"].get("content"),
                "tool_calls": [],
                **{
                    name: record[name]
                    for name in (
                        "finish_reason", "prompt_tokens", "cached_tokens",
                        "completion_tokens", "retries", "model_seconds",
                    )
                },
                "seconds": 0.0,
            }
            if record.get("empty"):
                step["empty"] = True
            steps[record["step"]] = step
            result[-1]["steps"].append(step)
        elif kind == "tool" and record.get("closed") != "not_started":
            steps[record["step"]]["tool_calls"].append({
                **{name: record[name] for name in (
                    "tool", "arguments", "result", "bad_call", "denied"
                )},
                **record.get("notes", {}),
            })
        elif kind == "step" and record["step"] in steps:
            step = steps[record["step"]]
            step["seconds"] = record["seconds"]
            # A log written before 0.6.2.8 says plugin_errors
            errors = record.get("hook_errors") or record.get("plugin_errors")
            if errors:
                step["hook_errors"] = errors
        elif kind == "turn_end":
            result[-1].update({
                OLD_LOG_FIELDS.get(name, name): record[name]
                for name in (
                    "outcome", "seconds", "agent_seconds", "wait_seconds",
                    "detail", "answer", "error", "hook_errors", "plugin_errors",
                )
                if name in record
            })
    return result


def unfinished_turn(records):
    """The unfinished turn's records, or an empty list."""
    start = None
    for i, record in enumerate(records):
        if record["type"] == "user" and not record.get("steer"):
            start = i
        elif record["type"] == "turn_end":
            start = None
    return records[start:] if start is not None else []


def parked_ask(records):
    """The open turn's `ask` record still waiting for its reply, or None."""
    ask = None
    for record in unfinished_turn(records):
        if record["type"] == "ask":
            ask = record
        elif record["type"] == "answer" and ask and record["ask"] == ask["ask"]:
            ask = None
    return ask


def lost_reply(records):
    """A reply that cancelled an ask but was cut off before it joined the turn as
    a message: its `answer` record, or None. The restart repair writes it in."""
    answer = None
    for record in unfinished_turn(records):
        if record["type"] == "answer":
            answer = None if record["yes"] else record
        elif answer and record["type"] == "user" and (
            record.get("inbox") == answer["inbox"]
        ):
            answer = None
    return answer


def dangling_calls(records):
    """Calls without a result, paired with their assistant record's step."""
    calls = {}
    for record in records:
        if record["type"] == "assistant":
            for call in record["message"].get("tool_calls") or []:
                calls[call["id"]] = (record["step"], call)
        elif record["type"] == "tool":
            calls.pop(record["call"], None)
    return list(calls.values())


def closing_results(records, result, closed):
    """Result records for unresolved calls, without inventing an execution."""
    for step, raw in dangling_calls(records):
        name = raw["function"]["name"]
        arguments = raw["function"]["arguments"]
        _, error = tools.check(name, arguments)
        yield {
            "step": step, "call": raw["id"], "tool": name, "arguments": arguments,
            "result": result, "bad_call": bool(error), "denied": False, "ran": False,
            "notes": {}, "closed": closed,
        }


def restart_note(records, now=None, extra=""):
    """Describe the gap before repair writes anything to the log: how long
    ago the turn was cut off, that nothing came from the user since, and
    `extra`, what the hooks add (the files this turn read or wrote that
    changed since): the facts the model judges from whether going on still
    makes sense; no age limit in code."""
    last = datetime.fromisoformat(records[-1]["time"])
    now = now or datetime.now().astimezone()
    seconds = max(0, int((now - last).total_seconds()))
    text = (
        f"This turn was cut off {span(seconds)} ago, when ZERO stopped; "
        "it has just restarted. Nothing has come from the user since."
    )
    return f"[{text}{extra}]"


def span(seconds):
    """45 seconds, 2 minutes, 14 hours, 3 days: a gap, for the model."""
    for size, unit, below in (
        (1, "second", 60), (60, "minute", 120 * 60), (3600, "hour", 48 * 3600),
    ):
        if seconds < below:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'}"
    count = seconds // 86400
    return f"{count} day{'' if count == 1 else 's'}"


def final_answer(records):
    last = None
    for record in reversed(records):
        if record["type"] == "user":
            return None
        if record["type"] == "assistant":
            last = record
            break
    if last and not last.get("empty") and last["finish_reason"] != "length":
        message = last["message"]
        if not message.get("tool_calls") and (message.get("content") or "").strip():
            return message["content"]
    return None
