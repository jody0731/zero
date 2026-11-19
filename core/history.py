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

import json
import re
from datetime import datetime

from core import tools

LOG_FORMAT = 7
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
COMPACTED = "[Compacted {when}: {what}{size}{asked}]"
# Who asked for a compaction with words on what its handoff keeps (its
# record's `instructions`), as the model reads it; the words follow quoted
ASKERS = {"user": "The user", "zero": "You"}
ASKED = "\n{whose} asked for it, with these words on what the handoff keeps:\n{words}"
CLEARED_ONLY = ("older outputs were saved whole and cleared from view, each in "
                "the file its line names.")
HANDED_OFF = "the conversation before the kept steps became the handoff above."
# A compaction that kept nothing but its handoff, on its asker's occasion
# (rounds/'s "a round ended")
FRESH = "the conversation before this point became the handoff above."
ON_OCCASION = "{occasion}, and {fresh}"
SIZE = " The context is now about {tokens:,} tokens of the {line:,} it is kept under."

# Where the user rewound, as the model reads it: what was left behind and
# what of it still stands; each part that changes things adds its lines
# (the `rewound` hooks), so the note names what the log knows exactly
REWOUND = (
    "[The user rewound this conversation. Continue from here. Files, memory "
    "and running work retain their current state.{effects}]"
)
# Older logs embedded excerpts and call counts in rewind and fork notes.
OLD_BRANCH_EXCERPTS = "\nThe user's messages there:"
# Inside a notice whose sender started on a part a rewind left behind
LEFT_BEHIND = (
    "[This started from record {seq} of this session's log, on a part of the "
    "conversation a rewind left behind.]"
)


class OldLog(ValueError):
    """The server must upgrade this log before handing it to the harness."""


class NewerLog(ValueError):
    """A newer ZERO wrote this log: this one neither runs nor upgrades it."""

    def __init__(self, found):
        super().__init__(
            f"this session's log was written by a newer ZERO (its format "
            f"{found}; this one reads {LOG_FORMAT}): update ZERO to open it")


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


def metered(meter, record, compact_at=None):
    """The user's meter of the context after `record`: a session or switch
    record sets the line it is kept under (`compact_at`, the
    compact_at_tokens setting as it stands, else the model's window, never
    past it: context/room.py's line); a model call its tokens (its prompt
    and reply) and the share of its prompt the server's cache served (None
    where the server doesn't say); a compaction the tokens it left, and no
    share until the next call."""
    meter = dict(meter or {"tokens": None, "line": None, "cached_share": None})
    kind = record.get("type")
    if kind == "session" or (kind in ("switch", "note") and is_switch(record)):
        settings = renamed(record.get("settings") or {})
        window = settings.get("context_limit")
        chosen = compact_at or window
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


def context_meter(records, compact_at=None):
    """The meter as a whole log has it (metered, record by record, under
    `compact_at`); None before the first model call."""
    meter = None
    for record in records:
        meter = metered(meter, record, compact_at)
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


def branch_note(record):
    """Render a saved branch note without replaying discarded conversation."""
    text = record["text"]
    if OLD_BRANCH_EXCERPTS not in text:
        return text
    if record["type"] == "rewind":
        return REWOUND.format(effects="")
    return "[This conversation forked. The workspace, files and memory are shared.]"


def log_format(records):
    """The format a log was written in: its session record's, else 1 (logs
    before formats were numbered). A newer ZERO's is its own number."""
    if records and records[0].get("type") == "session":
        found = records[0].get("format")
        if isinstance(found, int) and not isinstance(found, bool) and found > 1:
            return found
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
    elif record.get("source") == "mcp":
        text = f"[Message from {record['source']}]\n{text}"
    return f"{text}\n\n{SENT.format(when=when(record['time']))}"


def compacted(record):
    """A compaction where it happened: what it did and the size it left."""
    what = CLEARED_ONLY if record.get("cleared_only") else HANDED_OFF
    if record.get("fresh"):
        what = FRESH
        if record.get("occasion"):
            what = ON_OCCASION.format(occasion=record["occasion"], fresh=FRESH)
    size = ""
    if record.get("tokens_after") and record.get("line"):
        size = SIZE.format(tokens=record["tokens_after"], line=record["line"])
    asked = "".join(
        ASKED.format(whose=ASKERS.get(whose, whose), words=quoted(words))
        for whose, words in (record.get("instructions") or {}).items()
    )
    return COMPACTED.format(when=when(record["time"]), what=what, size=size,
                            asked=asked)


def quoted(text):
    """`text` as a quote, each line marked."""
    return "\n".join(f"> {line}" for line in text.strip().splitlines())


def message(record):
    """The message the model sees for one record, or None. A record that
    carries a system message after the session's first (a rebuild, a
    compaction) changes the context as a whole: `replay` places it; a
    compaction also says where it happened."""
    kind = record["type"]
    if kind == "session":
        return {"role": "system", "content": record["system_prompt"]}
    if kind == "answer" and record.get("as_message"):
        return {"role": "user", "content": sent(record)}
    if kind == "user" and record.get("mid_turn") and record.get("source") != "mcp":
        return {"role": "user", "content": f"{MID_TURN}\n{sent(record)}"}
    if kind == "user":
        return {"role": "user", "content": sent(record)}
    if kind == "rewind" or (kind == "note" and record.get("kind") == "forked"):
        return {"role": "user", "content": branch_note(record)}
    if kind == "note":
        return {"role": "user", "content": record["text"]}
    if kind == "compaction":
        return {"role": "user", "content": compacted(record)}
    if kind == "assistant" and not record.get("empty"):
        if "reply" in record:
            content = json.dumps(record["reply"], ensure_ascii=False,
                                 separators=(",", ":"))
            return {**record["message"], "content": content}
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
# them declares its own (jobs/tool.py's NOTICE_SENDERS), and the assembly adds
# them (add_notice_senders), so a new one (a subagent's report) is one more
# entry
NOTICES = {}


def add_notice_senders(senders):
    """Know these notices' senders from now on: source → what sends it, as
    the system message names it. The same again changes nothing; another
    sender under a known source is an error."""
    for source, what in senders.items():
        if NOTICES.get(source, what) != what:
            raise ValueError(f"notice source {source!r} declared twice")
        NOTICES[source] = what


def is_user_source(source):
    """The built-in human clients. An unfamiliar sender has no approval authority."""
    return source in ("web", "cli", "user") or (
        isinstance(source, str) and re.fullmatch(r"terminal-[0-9]+", source) is not None
    )


def requires_user(ask):
    """The original proposal stays human-only across a log or database upgrade."""
    notes = ask.get("notes") or {}
    return bool(notes.get("requires_user") or notes.get("approval_proposal"))


# Each input action has one delivery record and one kind of addressee.
# Other components send notices under their own declared source.
INPUT_ACTIONS = {
    "send": ("user", "session"), "steer": ("user", "session"),
    "queue": ("user", "session"), "continue": ("user", "session"),
    "notify": ("user", "session"), "answer": ("answer", "ask"),
    "approve": ("answer", "ask"), "decline": ("answer", "ask"),
    "ask": ("ask", "ask"),
}


def logged_interaction(record, session):
    """Construct input attribution at the writer. A caller cannot replace it."""
    if any(key in record for key in ("interaction", "sender", "action", "target")):
        raise ValueError("interaction is assigned by the session log")
    result = attributed(record, session)
    event = result.get("interaction")
    if event is None and ("source" in record or "reply_to" in record):
        raise ValueError(f"declare attribution for the new {record['type']} record")
    if event is not None and record["type"] in ("user", "answer", "ask"):
        kind, target = INPUT_ACTIONS[event["action"]]
        if record["type"] != kind or not event["target"].get(target):
            raise ValueError(f"{event['action']} requires a {kind} addressing {target}")
    return result


def interaction(record, session=None):
    """The same sender, action and addressed object on disk and in every view.

    Older records supply only their recorded facts. In particular a missing
    source cannot provide evidence of who approved a change.
    """
    if "interaction" in record:
        return record["interaction"]
    kind = record.get("type")
    target = {"session": session} if session else {}
    sender = record.get("source") or "unknown"
    if kind == "user":
        if is_notice(record):
            action = "notify"
            if record.get("reference"):
                target["reference"] = record["reference"]
        elif record.get("continue"):
            action = "continue"
        elif record.get("steer"):
            action = "steer"
            if record.get("turn_inbox"):
                target["turn"] = record["turn_inbox"]
        elif record.get("queued"):
            action = "queue"
        else:
            action = "send"
    elif kind == "answer":
        action = "answer"
        if record.get("decision") in ("approve", "decline"):
            action = record["decision"]
        elif record.get("kind") not in ("question", "secret"):
            if record.get("yes"):
                action = "approve"
            elif record.get("choice") == "no":
                action = "decline"
        if record.get("ask"):
            target["ask"] = record["ask"]
    elif kind == "ask":
        sender, action = "zero", "ask"
        if record.get("ask"):
            target["ask"] = record["ask"]
    elif kind == "tool":
        sender, action = record.get("tool") or "unknown", "result"
        if record.get("call"):
            target["call"] = record["call"]
    elif kind in ("assistant", "compaction"):
        sender = "zero"
        action = "reply" if kind == "assistant" else "summarize"
    else:
        return None
    result = {"sender": sender, "action": action, "target": target}
    if record.get("sender_session"):
        result["sender_session"] = record["sender_session"]
    return result


def sender_name(sender, sender_session=None):
    if sender_session:
        return f"{sender} {sender_session}"
    if is_user_source(sender):
        return "You"
    return {"mcp": "Agent", "zero": "ZERO", "unknown": "Unknown sender"}.get(
        sender, sender)


def attributed(record, session=None):
    details = interaction(record, session)
    if details is None or "interaction" in record:
        return record
    return {**record, "interaction": details}


def ancestor_cutoffs(session, header, read_header, read_records):
    """Each ancestor's last inherited seq, even after intermediate forks are gone."""
    limits = {}
    fallback = set()
    ceiling = None
    while isinstance(header, dict):
        fork = header.get("forked_from") or {}
        if not isinstance(fork, dict):
            break
        parent, seq = fork.get("session"), fork.get("seq")
        if (not isinstance(parent, str) or "/" in parent or parent in limits
                or type(seq) is not int):
            break
        ceiling = seq if ceiling is None else min(ceiling, seq)
        limits[parent] = ceiling
        fallback.update(sid for sid in fork.get("ancestors") or ()
                        if isinstance(sid, str) and "/" not in sid)
        saved = fork.get("ancestor_cutoffs")
        if isinstance(saved, dict):
            for sid, end in saved.items():
                if type(end) is int:
                    limits.setdefault(sid, min(end, ceiling))
            break
        header = read_header(parent)
    # Old headers named every ancestor but kept only the direct parent's cutoff.
    # Beyond a deleted intermediate, only matching copied records prove a limit.
    missing = fallback - limits.keys()
    if missing:
        copied = [r for r in read_records(session) if 1 < r.get("seq", 0) <= ceiling]
        for sid in missing:
            original = {r.get("seq"): r for r in read_records(sid)}
            limits[sid] = 0
            for row in copied:
                before = original.get(row["seq"])
                if before is None:
                    break
                # Forking may have added attribution to a legacy source record.
                if row != before and ({**row, "interaction": None}
                                      != {**before, "interaction": None}):
                    break
                limits[sid] = row["seq"]
    return limits


def attributed_records(records, sid, read_header):
    """Resolve legacy targets from fork headers. New records carry their own target."""
    origins = []
    fork = (records[0].get("forked_from") or {}) if records else {}
    if fork and any("interaction" not in row and row["type"] in (
            "user", "ask", "answer", "tool", "assistant", "compaction")
            for row in records):
        visited = {sid}
        while fork:
            parent, cutoff = fork.get("session"), fork.get("seq")
            if not (isinstance(parent, str) and "/" not in parent
                    and parent not in visited and isinstance(cutoff, int)):
                break
            visited.add(parent)
            origins.append((cutoff, parent))
            if len(fork.get("ancestors", [parent])) <= 1:
                break
            head = read_header(parent)
            if head is None:
                # A deleted ancestor leaves several possible original sessions.
                origins[-1] = (cutoff, None)
                break
            fork = head.get("forked_from")
    for record in records:
        origin = sid
        for cutoff, parent in origins:
            if record.get("seq", 0) > cutoff:
                break
            origin = parent
        yield attributed(record, origin)


def is_notice(message):
    """Whether a message, a user record or a steer, is a notice."""
    return message.get("source") in NOTICES


# What a message to a session may carry beside its text, kept on its user
# record: the record its sender started from (`started_from_seq`, for a
# notice from a part a rewind left behind) and, for a notice that quotes
# output, what it says without it (`summary`, which the judge reads)
MESSAGE_FIELDS = ("started_from_seq", "summary", "reference", "sender_session")


def steer_fields(steer):
    """A message a running turn takes in, as its record's fields. The user's
    own are `mid_turn`, so the model reads that they came while it
    worked; a notice says what it is itself."""
    fields = {"text": steer["text"], "steer": True}
    for name in ("inbox", "source", *MESSAGE_FIELDS):
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
    kept = compaction["kept_from"] if compaction else 0
    if compaction and kept:
        # An older compactor could cut at a notice inside a tool batch.
        pending = {}
        for record in records:
            if record["seq"] >= kept:
                break
            if record["type"] == "assistant":
                pending = {call["id"]: record["seq"] for call in
                           record["message"].get("tool_calls") or ()}
            elif record["type"] == "tool":
                pending.pop(record.get("call"), None)
        if pending:
            kept = min(pending.values())
    return [r for r in without_question_echoes(records)
            if r["seq"] >= kept and r["type"] not in ("session", "system")]


def handoff(compaction):
    """A compaction's handoff as the model reads it, or None for one that
    only cleared outputs and had none to carry."""
    if not compaction.get("summary"):
        return None
    head = HANDOFF.format(when=when(compaction.get("handoff_at")
                                    or compaction["time"]))
    return {"role": "user", "content": f"{head}\n\n{compaction['summary']}"}


def append_message(messages, message):
    """Keep tool results together if a notice arrived while a call was parked."""
    if message["role"] == "tool" and messages and messages[-1]["role"] == "user":
        position = len(messages)
        while position and messages[position - 1]["role"] == "user":
            position -= 1
        start = position
        while start and messages[start - 1]["role"] == "tool":
            start -= 1
        calls = messages[start - 1].get("tool_calls") if start else None
        if any(call["id"] == message["tool_call_id"] for call in calls or ()):
            messages.insert(position, message)
            return
    messages.append(message)


def without_question_echoes(records):
    """Older question replies also logged the prompt and choice as a user steer."""
    answered = set()
    for record in records:
        if (record["type"] == "answer" and record.get("kind") == "question"
                and record.get("inbox")):
            answered.add(record["inbox"])
        if (record["type"] == "user" and record.get("steer")
                and record.get("inbox") in answered):
            continue
        yield record


def transcript(records):
    """Old reply echoes can follow tool results, even across a page boundary."""
    answers = set()
    for record in records:
        if record["type"] == "answer" and record.get("inbox"):
            answers.add(record["inbox"])
        if (record["type"] == "user" and record.get("steer")
                and record.get("inbox") in answers):
            continue
        yield record


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
        append_message(messages, found)
    return messages


def turns(records):
    """Turns in the trajectory's shape; a restart keeps the same turn."""
    result = []
    steps = {}
    for record in without_question_echoes(records):
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
            if "reply" in record:
                step["reply"] = record["reply"]
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
                    "detail", "answer", "reply", "error", "hook_errors",
                    "plugin_errors",
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
            cancelled = (not record["yes"] and record.get("kind") != "question"
                         and "interaction" not in record
                         and record.get("choice") != "no")
            answer = record if cancelled else None
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


def question_result(record, text):
    event = interaction(record)
    sender = event["sender"]
    if is_user_source(sender):
        return text
    return f"[Answer from {event.get('sender_session') or sender}]\n{text}"


def closing_results(records, result, closed):
    """Result records for unresolved calls, without inventing an execution."""
    asks = {r["ask"]: (r["step"], r["call"]) for r in records
            if r["type"] == "ask" and r.get("kind") == "question"}
    answers = {asks[r["ask"]]: question_result(r, r["text"])
               for r in records if r["type"] == "answer"
               and r.get("kind") == "question" and r["ask"] in asks}
    for step, raw in dangling_calls(records):
        name = raw["function"]["name"]
        arguments = raw["function"]["arguments"]
        # A call through a tool that calls another by name is that tool's,
        # as its record would have said (core/records.py)
        through = {}
        inner = tools.unwrapped(name, arguments)
        if inner is not None:
            through = {"called_through": name}
            name, arguments = inner
        _, error = tools.check(name, arguments)
        fields = {
            "step": step, "call": raw["id"], "tool": name, "arguments": arguments,
            "result": result, "bad_call": bool(error), "denied": False, "ran": False,
            "notes": {}, "closed": closed, **through,
        }
        if (step, raw["id"]) in answers:
            fields.update(result=answers[step, raw["id"]], ran=True)
            del fields["closed"]
        yield fields


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


def ended_fresh(records):
    """A turn's reply before the fresh compaction its end wrote (a round's
    end): nothing of the turn goes on after one, so a turn cut off past it
    has ended, with this answer ("" for none). None if it holds none."""
    for at in range(len(records) - 1, -1, -1):
        record = records[at]
        if record["type"] == "compaction" and record.get("fresh"):
            replies = [r for r in records[:at] if r["type"] == "assistant"]
            content = replies[-1]["message"].get("content") if replies else None
            return (content or "").strip()
        if record["type"] in ("assistant", "tool", "user"):
            return None
    return None


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
        if not message.get("tool_calls") and (
                "reply" in last or (message.get("content") or "").strip()):
            return message["content"]
    return None
