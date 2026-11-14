"""What the protocol's replies carry, built from the store and the logs in
one place: a session's row, an ask, and a log's records as an app reads
them (protocol/messages.py's ROWS and RECORDS).

A row is built from the store's columns by name, field by field, so a column
renamed in the store (server/store.py's RENAMED_COLUMNS) changes the
names below and no reply; a column an app has no use for (the next
turn's switch, the log's place) never reaches one. A record goes out as the
log holds it, with what an app would otherwise work out from another part's
words: each tool call's line as an ask shows it (`call_summaries`), and an
ask's answers with their words (`choice_labels`). A component's own rows
(a job, a run in rounds) are built by its service, by the same rule.
"""

import json

from core import asking, tools


def ask_row(ask):
    """An open ask, from its row in the asks table."""
    labels = json.loads(ask.get("choice_labels") or "null")
    if not labels:
        labels = asking.choice_labels(asking.PLAIN_CHOICES)
    return {
        "id": ask["id"], "tool": ask["tool"], "command": ask["command"],
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


def call_summaries(message):
    """Each of a model message's tool calls by its id, as an ask shows it;
    the raw arguments where they aren't JSON."""
    found = {}
    for call in message.get("tool_calls") or ():
        function = call.get("function") or {}
        name, arguments = function.get("name", ""), function.get("arguments", "")
        try:
            args = json.loads(arguments)
        except (TypeError, ValueError):
            args = None
        if isinstance(args, dict):
            try:
                found[call.get("id", "")] = tools.summary(name, args, arguments)
                continue
            except Exception:
                pass
        found[call.get("id", "")] = f"{name} {arguments}"
    return found


def record(record):
    """A log's record as an app reads it."""
    kind = record.get("type")
    if kind == "assistant" and (record.get("message") or {}).get("tool_calls"):
        return {**record, "call_summaries": call_summaries(record["message"])}
    if kind == "ask":
        offer = (record.get("notes") or {}).get("offer_text")
        choices = record.get("choices") or asking.PLAIN_CHOICES
        return {**record, "choice_labels": asking.choice_labels(choices, offer)}
    return record


def records(found):
    return [record(r) for r in found]


def event(event):
    """A watch's event as an app reads it: its record, if it has one, built
    as above."""
    if "record" in event:
        return {**event, "record": record(event["record"])}
    return event
