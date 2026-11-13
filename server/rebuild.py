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

from dataclasses import dataclass, field
from datetime import datetime

from core import history

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
    if not records or records[0].get("type") != "session":
        return None
    header = records[0]
    row = dict(header.get("row") or {})
    if "title" not in row or "source" not in row:
        first = next((r for r in records if r["type"] == "user"
                      and not r.get("steer")), None)
        text = (first or {}).get("text", "").strip()
        row.setdefault("title", title(text) if text else sid)
        row.setdefault("source", (first or {}).get("source") or ADOPTED_SOURCE)
    forked = header.get("forked_from") or {}
    if forked:
        row.setdefault("parent", forked.get("session"))
        row.setdefault("started_by", forked.get("started_by"))
    settings = header.get("settings") or {}
    now = history.running_with(records)
    row["model"] = now.get("model") or header.get("model")
    row["mode"] = now.get("mode") or header.get("mode") or settings.get("mode")
    row["workspace"] = (now.get("workspace") or settings.get("workspace") or "/")
    row.setdefault("created_at", timestamp(header.get("time")))
    row["updated_at"] = timestamp(records[-1].get("time"))
    return row


@dataclass
class KernelRows:
    # idle, running, waiting or failed, as the log's last turn left it
    state: str = "idle"
    # The newest turn_end's seq, and the newest record's
    end_seq: int = 0
    log_seq: int = 0
    # Every ask the log holds, as the asks table keeps it
    asks: list = field(default_factory=list)
    # Each inbox message the log names: id → delivered or done
    inbox: dict = field(default_factory=dict)


def timestamp(iso):
    return datetime.fromisoformat(iso).timestamp() if iso else None


def state_of(records):
    """What the branch's last turn left the session: waiting on an ask,
    running (cut off), failed, or idle."""
    if history.unfinished_turn(records):
        return "waiting" if history.parked_ask(records) else "running"
    ends = [r for r in records if r["type"] == "turn_end"]
    return "failed" if ends and ends[-1].get("outcome") in FAILED else "idle"


def kernel_rows(records):
    """The kernel's rows from a session's whole log (every branch: a turn a
    rewind left behind still happened), in the current format."""
    rows = KernelRows()
    if not records:
        return rows
    branch = history.branch(records)
    rows.state = state_of(branch)
    rows.log_seq = records[-1].get("seq", 0)
    parked = history.parked_ask(branch)
    answers = {}
    for record in records:
        if record["type"] == "answer":
            answers.setdefault(record.get("ask"), record)
    begun, done = [], set()
    # The inbox id of the turn under way, as the log is read
    turn = None
    for record in records:
        kind, inbox = record["type"], record.get("inbox")
        if kind == "user" and not record.get("steer"):
            turn = inbox
            if inbox is not None:
                begun.append(inbox)
        elif kind == "user" and inbox is not None:
            done.add(inbox)
        elif kind == "turn_end":
            rows.end_seq = max(rows.end_seq, record.get("seq", 0))
            if inbox is not None:
                done.add(inbox)
            turn = None
        elif kind == "answer" and inbox is not None:
            done.add(inbox)
        elif kind == "ask":
            rows.asks.append(ask_row(record, turn, answers.get(record["ask"]),
                                     parked))
    rows.inbox = {id: "delivered" for id in begun}
    rows.inbox.update({id: "done" for id in done})
    return rows


def ask_row(ask, turn, answer, parked):
    """An `ask` record as the asks table keeps it: closed by its answer; open
    while its turn waits on it; else cancelled (its turn ended without
    one)."""
    if answer is not None:
        status = "approved" if answer.get("yes") else "cancelled"
    elif parked is not None and parked["ask"] == ask["ask"]:
        status = "open"
    else:
        status = "cancelled"
    return {
        "id": ask["ask"], "call": ask.get("call") or "", "turn": turn or "",
        "tool": ask.get("tool") or "", "command": str(ask.get("command", "")),
        "why": ask.get("why") or "", "reason": ask.get("reason") or "",
        "about": ask.get("about"), "status": status,
        "created_at": timestamp(ask.get("time")) or 0.0,
        "closed_at": timestamp(answer.get("time")) if answer else None,
        "reply": answer.get("inbox") if answer else None,
        "source": answer.get("source") if answer else None,
    }
