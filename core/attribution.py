"""The sender, action and target of a message, ask, reply or notice."""

import re
from collections.abc import Mapping
from dataclasses import replace

from core import entries
from core.entries import Entry

# Components declare these senders at assembly time.
NOTICES = {}


def add_notice_senders(senders):
    """Register what each notice's source means to the model."""
    for source, what in senders.items():
        if NOTICES.get(source, what) != what:
            raise ValueError(f"notice source {source!r} declared twice")
        NOTICES[source] = what


def is_user_source(source):
    """An unfamiliar sender has no approval authority."""
    return source in ("web", "cli", "user") or (
        isinstance(source, str) and re.fullmatch(r"terminal-[0-9]+", source) is not None
    )


def is_user_message(origin, source):
    """The words a human client sent as a user's message."""
    return origin == "user" and is_user_source(source)


def requires_user(ask):
    """The original proposal stays human-only across a log or database upgrade."""
    notes = ask.get("notes") or {}
    return bool(notes.get("requires_user") or notes.get("approval_proposal"))


def is_notice(message):
    """Whether a delivered entry or an incoming message came from a notice sender."""
    return message.get("source") in NOTICES


def is_turn_input(entry):
    """The messages and notices delivered to a turn, including continuations."""
    return entries.has(entry, "user_message") or entries.has(entry, "notice")


def delivered_text(entry):
    """A delivered notice may carry the original text as quoted tool evidence."""
    if entries.has(entry, "notice"):
        evidence = next((child for child in entry.entries or ()
                         if child.origin == "tool"), None)
        if evidence is not None:
            return entries.text(evidence)
    return entries.text(entry)


def _interaction(entry, session):
    if entry.fields and "interaction" in entry.fields:
        return entry.get("interaction")
    target = {"session": session} if session else {}
    sender = entry.get("source") or "unknown"
    if is_turn_input(entry):
        if is_notice(entry):
            action = "notify"
            if reference := entry.get("reference"):
                target["reference"] = reference
        elif entry.get("continue"):
            action = "continue"
        elif entry.get("steer"):
            action = "steer"
            if turn := entry.get("turn_inbox"):
                target["turn"] = turn
        elif entry.get("queued"):
            action = "queue"
        else:
            action = "send"
    elif entries.has(entry, "answer"):
        action = "answer"
        if entry.get("decision") in ("approve", "decline"):
            action = entry.get("decision")
        elif entry.get("kind") in (None, "permission"):
            if entry.get("yes"):
                action = "approve"
            elif entry.get("choice") == "no":
                action = "decline"
        if ask := entries.linked_to(entry, "answers"):
            target["ask"] = ask
    elif entries.has(entry, "ask"):
        sender, action = "zero", "ask"
        if entry.id is not None:
            target["ask"] = entry.id
    elif entries.has(entry, "tool_result"):
        sender, action = entry.get("tool") or "unknown", "result"
        if call := entries.linked_to(entry, "answers"):
            target["call"] = call
    elif entries.has(entry, "assistant_reply"):
        sender, action = "zero", "reply"
    elif entries.has(entry, "context_boundary"):
        sender, action = "zero", "summarize"
    else:
        return None
    result = {"sender": sender, "action": action, "target": target}
    if sender_session := entry.get("sender_session"):
        result["sender_session"] = sender_session
    return result


# Each input action has one delivery trait and one kind of addressee.
INPUT_ACTIONS = {
    "send": ("input", "session"), "steer": ("input", "session"),
    "queue": ("input", "session"), "continue": ("input", "session"),
    "notify": ("input", "session"), "answer": ("answer", "ask"),
    "approve": ("answer", "ask"), "decline": ("answer", "ask"),
    "ask": ("ask", "ask"),
}


def logged_interaction(entry: Entry, session):
    """Assign input attribution at the writer, before the entry is persisted."""
    fields = entry.fields or {}
    if fields.keys() & {"interaction", "sender", "action", "target"}:
        raise ValueError("interaction is assigned by the session log")
    event = _interaction(entry, session)
    if event is None:
        if fields.keys() & {"source", "reply_to"}:
            raise ValueError(f"declare attribution for the new {entry.type} entry")
        return entry
    if action := INPUT_ACTIONS.get(event["action"]):
        trait, target = action
        matches = (
            is_turn_input(entry) if trait == "input" else entries.has(entry, trait)
        )
        if not matches or not event["target"].get(target):
            raise ValueError(
                f"{event['action']} requires a {trait} addressing {target}"
            )
    return replace(entry, fields={**fields, "interaction": event})


def interaction(record, session=None):
    """Resolve the sender, action and target from the record's saved facts."""
    return _interaction(record.entry, session)


def sender_name(sender, sender_session=None):
    if sender_session:
        return f"{sender} {sender_session}"
    if is_user_source(sender):
        return "You"
    return {"mcp": "Agent", "zero": "ZERO", "unknown": "Unknown sender"}.get(
        sender, sender,
    )


def attributed(record, session=None):
    entry = record.entry
    if entry.fields and "interaction" in entry.fields:
        return record
    event = _interaction(entry, session)
    if event is None:
        return record
    fields = {**(entry.fields or {}), "interaction": event}
    return replace(record, entry=replace(entry, fields=fields))


def _inherited(row, original):
    if original is None or row.parent != original.parent or row.time != original.time:
        return False
    copied, before = row.entry, original.entry
    if (copied.type, copied.entries, copied.links) != (
        before.type, before.entries, before.links
    ):
        return False
    # An older source may have gained attribution when the fork copied it.
    return {k: v for k, v in (copied.fields or {}).items() if k != "interaction"} == {
        k: v for k, v in (before.fields or {}).items() if k != "interaction"
    }


def ancestor_cutoffs(session, header, read_header, read_records):
    """Each ancestor's last inherited seq, even after intermediate forks are gone."""
    limits = {}
    fallback = set()
    ceiling = None
    while header is not None:
        fork = header.entry.get("forked_from") or {}
        if not isinstance(fork, Mapping):
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
        if isinstance(saved, Mapping):
            for sid, end in saved.items():
                if type(end) is int:
                    limits.setdefault(sid, min(end, ceiling))
            break
        header = read_header(parent)
    # An older header kept only the direct parent's cutoff.
    missing = fallback - limits.keys()
    if missing:
        copied = [row for row in read_records(session) if 1 < row.seq <= ceiling]
        for sid in missing:
            original = {row.seq: row for row in read_records(sid)}
            limits[sid] = 0
            for row in copied:
                if not _inherited(row, original.get(row.seq)):
                    break
                limits[sid] = row.seq
    return limits


def record_sessions(records, sid, read_header, *, header=None):
    """The original session of each record, inferred from legacy fork headers."""
    origins = []
    if header is None and records:
        header = records[0]
    fork = (header.entry.get("forked_from") or {}) if header is not None else {}
    traits = ("ask", "answer", "tool_result", "assistant_reply", "context_boundary")
    if fork and any(
        "interaction" not in (row.entry.fields or {})
        and (is_turn_input(row.entry)
             or any(entries.has(row.entry, trait) for trait in traits))
        for row in records
    ):
        visited = {sid}
        while isinstance(fork, Mapping) and fork:
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
            fork = head.entry.get("forked_from")
    for record in records:
        origin = sid
        for cutoff, parent in origins:
            if record.seq > cutoff:
                break
            origin = parent
        yield record, origin


def attributed_records(records, sid, read_header):
    """Resolve a legacy record's target through any headers it was copied from."""
    for record, origin in record_sessions(records, sid, read_header):
        yield attributed(record, origin)


# Fields an incoming message may carry in addition to its text.
MESSAGE_FIELDS = ("started_from_seq", "summary", "reference", "sender_session")


def steer_fields(steer):
    """The record fields for a message delivered while a turn is running."""
    fields = {"text": steer["text"], "steer": True}
    for name in ("inbox", "source", *MESSAGE_FIELDS):
        if steer.get(name) is not None:
            fields[name] = steer[name]
    if not is_notice(steer):
        fields["mid_turn"] = True
    return fields


def question_result(answer: Entry, text):
    event = _interaction(answer, None)
    sender = event["sender"]
    if is_user_source(sender):
        return text
    return f"[Answer from {event.get('sender_session') or sender}]\n{text}"
