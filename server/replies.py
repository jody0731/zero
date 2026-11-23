"""What the protocol's replies carry, built from the store and the logs in
one place: a session's row, an ask, and a log's records as Protocol 8 apps
read them (protocol/messages.py's ROWS and RECORD).

A row is built from the store's columns by name, field by field, so a column
renamed in the store (server/store.py's RENAMED_COLUMNS) changes the
names below and no reply; a column an app has no use for (the next
turn's switch, the log's place) never reaches one. Timeline records travel
solely as native public `Entry` envelopes plus server-computed display hints
(`audience`, `sender_name`, `notice_source`, `call_summaries`). A component's
own rows (a job, a run in rounds) are built by its service.
"""

import json

from core import asking, attribution, entries, timeline, tools


def ask_row(ask):
    """An open ask, from its row in the asks table."""
    labels = json.loads(ask.get("choice_labels") or "null")
    if labels is None:
        labels = asking.choice_labels(asking.PLAIN_CHOICES)
    return {
        "id": ask["id"], "tool": ask["tool"], "command": ask["command"],
        "kind": ask.get("kind") or "permission",
        "why": ask["why"], "reason": ask["reason"], "about": ask["about"],
        "created_at": ask["created_at"],
        "choices": list(labels), "choice_labels": labels,
    }


# A session row's fields by the sessions table's columns they come from:
# the one place outside the store that names them, so a column renamed
# there is renamed here and in no reply
SESSION_COLUMNS = {
    "id": "id", "title": "title", "source": "source", "model": "model",
    "mode": "mode", "workspace": "workspace", "state": "state",
    "created_at": "created_at", "updated_at": "updated_at",
    "parent": "parent", "started_by": "started_by",
}
# The columns the fields worked out from a row read
ARCHIVED_COLUMN, TURN_END_COLUMN, SEEN_COLUMN = "archived", "end_seq", "seen_seq"


def session_row(row, ask=None, pending=()):
    """A session, from its row in the sessions table: `ask` its open ask's
    row (or None), `pending` what a running or waiting turn still holds
    from before a set. The services add their own fields after (jobs,
    rounds)."""
    found = {field: row[column] for field, column in SESSION_COLUMNS.items()}
    found["archived"] = bool(row[ARCHIVED_COLUMN])
    found["unseen"] = row[TURN_END_COLUMN] > row[SEEN_COLUMN]
    found["pending"] = list(pending)
    found["ask"] = None if ask is None else ask_row(ask)
    return found


def call_summaries(entry):
    """Each of a model reply's tool calls by its id, as an ask shows it;
    the raw arguments where they aren't JSON."""
    found = {}
    for call in entries.tool_calls(entry):
        identifier = entries.call_id(call) or ""
        name = call.get("name") or ""
        arguments = call.get("arguments") or ""
        try:
            args = json.loads(arguments)
        except (TypeError, ValueError):
            args = None
        if isinstance(args, dict):
            try:
                found[identifier] = tools.summary(name, args, arguments)
                continue
            except Exception:
                pass
        found[identifier] = f"{name} {arguments}"
    return found


def record(record, *, session=None):
    """A timeline record as Protocol 8 apps read it, or None if wholly private."""
    entry = record.entry
    public = entries.public(entry)
    if public is None:
        return None
    fields = dict(public.get("fields") or {})
    if entries.has(entry, "header") and "format" not in fields:
        fields["format"] = (
            timeline.LOG_FORMAT if record.log_format is None
            else record.log_format
        )
    if "interaction" not in fields:
        if details := attribution.interaction(record, session):
            fields["interaction"] = entries.thaw(details)
    if reply := fields.get("reply"):
        next_steps = reply.get("next") or {}
        if not any((value or "").strip() for value in (
            reply.get("why"), next_steps.get("user"), next_steps.get("zero"),
        )):
            fields.pop("reply")
    if entries.has(entry, "ask") and fields.get("kind") in (None, "permission"):
        notes = entry.get("notes") or {}
        choices = fields.get("choices") or list(asking.PLAIN_CHOICES)
        fields["choices"] = list(choices)
        fields["choice_labels"] = asking.choice_labels(
            choices, notes.get("offer_text"), notes.get("session_scope"),
        )
    if fields:
        public["fields"] = fields
    else:
        public.pop("fields", None)
    found = {"seq": record.seq, "time": record.time, "entry": public}
    if record.parent is not None:
        found["parent"] = record.parent
    if details := fields.get("interaction"):
        found["sender_name"] = attribution.sender_name(
            details["sender"], details.get("sender_session"),
        )
    if (entries.has(entry, "note") or entries.has(entry, "switch")) and (
        "model_failure" not in fields
    ):
        # Harness notes belong to the model's context. Operational state
        # reaches people through turn outcomes, asks, jobs and session rows,
        # or a fallback note with a model failure report.
        found["audience"] = "model"
    elif entries.has(entry, "notice") and fields.get("source") in attribution.NOTICES:
        found["notice_source"] = attribution.NOTICES[fields["source"]]
    elif entries.has(entry, "assistant_reply"):
        if summaries := call_summaries(entry):
            found["call_summaries"] = summaries
    return found


def records(found):
    return [wire for r in found if (wire := record(r)) is not None]


def event(event):
    """A watch's event as an app reads it: its record projected to Protocol 8,
    or None when the record is wholly private."""
    if "record" in event:
        found = dict(event)
        session = found.pop("record_session", None)
        wire = record(found["record"], session=session)
        if wire is None:
            return None
        found["record"] = wire
        return found
    return event
