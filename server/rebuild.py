"""The rows a session's log says (design rule 9: the log is the truth, and
the database beside it is rebuilt from it).

What the log holds of the kernel's rows: the session's state and its newest
turn's end, its asks, and the status of each inbox message the log names.
The inbox itself, what was sent and hasn't happened yet, and what the user
set (a title, an archive, what they have seen, a switch for the next turn)
are the database's own. Each server service declares its share
(`Service.rebuild`). A session's row keeps the newest record its rows are in
step with (`log_seq`); at start, only the sessions whose log is ahead of
that are rebuilt (server/runtime.py), each in one transaction
(Store.rebuild).
"""

import json
from dataclasses import dataclass, field
from datetime import datetime

from core import asking, attribution, entries, timeline

# A turn's outcomes that leave its session failed
FAILED = ("crashed", "cut_off", "failed")
# What a session's row holds that its log's header carries, under "row"
# (7.5.2), so a log with no row is listed again from it; what it runs with
# (model, mode, workspace) the log already says
HEADER_ROW_FIELDS = ("title", "source", "parent", "started_by", "created_at")
# Who a log from before 7.5.2 is listed as from, with no row in its header
# and no message of the user's (its title is then its id)
ADOPTED_SOURCE = "found"


def header_row(row):
    """What of a session's row its log's header carries."""
    return {name: row[name] for name in HEADER_ROW_FIELDS if row.get(name) is not None}


def adopted_row(sid, records, title):
    """The row of session `sid`, whose log has none (design rule 9), from
    its records in the current format: its header's row, else (a log from
    before 7.5.2) its first message's words and sender, `title(text)` its
    title (its id, with no message); what it runs with as the log says.
    None for a log with no header, not a session's."""
    if not records or not entries.has(records[0].entry, "header"):
        return None
    header = records[0].entry
    row = dict(header.get("row") or {})
    if "title" not in row or "source" not in row:
        first = next((r.entry for r in records if attribution.is_turn_input(r.entry)
                      and not r.entry.get("steer")), None)
        text = attribution.delivered_text(first).strip() if first else ""
        source = first.get("source") if first else None
        row.setdefault("title", title(text) if text else sid)
        row.setdefault("source", source or ADOPTED_SOURCE)
    forked = header.get("forked_from") or {}
    if forked:
        row.setdefault("parent", forked.get("session"))
        row.setdefault("started_by", forked.get("started_by"))
    settings = header.get("settings") or {}
    now = timeline.running_with(records)
    row["model"] = now.get("model") or header.get("model")
    row["mode"] = now.get("mode") or header.get("mode") or settings.get("mode")
    row["workspace"] = (now.get("workspace") or settings.get("workspace") or "/")
    row.setdefault("created_at", timestamp(records[0].time))
    row["updated_at"] = timestamp(records[-1].time)
    return row


@dataclass
class KernelRows:
    # idle, running, waiting or failed, as the log's last turn left it
    state: str = "idle"
    # The newest turn_end's seq, and the newest record's
    end_seq: int = 0
    log_seq: int = 0
    # The original message that owns the current unfinished turn.
    open_turn: timeline.LogRecord | None = None
    # Every ask the log holds, as the asks table keeps it
    asks: list = field(default_factory=list)
    # Each inbox message the log names: id → delivered or done
    inbox: dict = field(default_factory=dict)


def labels_of(ask):
    """An `ask` record's answers with their words, as the asks table keeps
    them (JSON)."""
    entry = ask.entry
    if entry.get("kind") not in (None, "permission"):
        return json.dumps(entries.thaw(entry.get("choice_labels")))
    notes = entry.get("notes") or {}
    labels = asking.choice_labels(entry.get("choices") or asking.PLAIN_CHOICES,
                                  notes.get("offer_text"), notes.get("session_scope"))
    return json.dumps(labels)


def timestamp(iso):
    return datetime.fromisoformat(iso).timestamp() if iso else None


def state_of(records):
    """What the branch's last turn left the session: waiting on an ask,
    running (cut off), failed, or idle."""
    pending = timeline.unfinished_turn(records)
    if pending:
        ask = timeline.parked_ask(pending, current_turn=True)
        return "waiting" if ask is not None else "running"
    end = next((r.entry for r in reversed(records) if entries.has(r.entry, "turn_end")),
               None)
    return "failed" if end and end.get("outcome") in FAILED else "idle"


def kernel_rows(records):
    """The kernel's rows from a session's whole log (every branch: a turn a
    rewind left behind still happened), in the current format."""
    rows = KernelRows()
    if not records:
        return rows
    branch = timeline.branch(records)
    rows.state = state_of(branch)
    rows.log_seq = records[-1].seq
    pending = timeline.unfinished_turn(branch)
    parked = timeline.parked_ask(pending, current_turn=True)
    answers = {}
    for record in records:
        if entries.has(record.entry, "answer"):
            answers.setdefault(entries.linked_to(record.entry, "answers"), record)
    begun, done = [], set()
    # The inbox id of the turn under way, as the log is read
    turn = None
    inherited = (records[0].entry.get("forked_from") or {}).get("seq", 0)
    if pending and pending[0].seq > inherited and pending[0].entry.get("inbox"):
        rows.open_turn = pending[0]
    for record in records:
        if record.seq <= inherited:
            continue
        entry = record.entry
        inbox = entry.get("inbox")
        if attribution.is_turn_input(entry):
            if not entry.get("steer"):
                turn = inbox
                if inbox is not None:
                    begun.append(inbox)
            elif inbox is not None:
                done.add(inbox)
        elif entries.has(entry, "turn_end"):
            rows.end_seq = max(rows.end_seq, record.seq)
            if inbox is not None:
                done.add(inbox)
            turn = None
        elif entries.has(entry, "answer") and inbox is not None:
            done.add(inbox)
        elif entries.has(entry, "ask"):
            rows.asks.append(ask_row(record, turn, answers.get(entry.id), parked))
    rows.inbox = {id: "delivered" for id in begun}
    rows.inbox.update({id: "done" for id in done})
    return rows


def ask_row(ask, turn, answer, parked):
    """An `ask` record as the asks table keeps it: closed by its answer; open
    while its turn waits on it; else cancelled (its turn ended without
    one)."""
    entry = ask.entry
    answered = answer.entry if answer is not None else None
    if answered is not None:
        status = ("answered" if entry.get("kind") not in (None, "permission") else
                  "approved" if answered.get("yes") else "cancelled")
    elif parked is not None and parked.entry.id == entry.id:
        status = "open"
    else:
        status = "cancelled"
    return {
        "id": entry.id, "call": entries.linked_to(entry, "asks_about") or "",
        "turn": turn or "", "tool": entry.get("tool") or "",
        "command": str(entry.get("command", "")), "why": entry.get("why") or "",
        "reason": entry.get("reason") or "", "about": entry.get("about"),
        "status": status, "created_at": timestamp(ask.time) or 0.0,
        "closed_at": timestamp(answer.time) if answer else None,
        "reply": answered.get("inbox") if answered else None,
        "source": answered.get("source") if answered else None,
        "choice_labels": labels_of(ask),
        "kind": entry.get("kind", "permission"),
        "requires_user": attribution.requires_user(entry),
    }
