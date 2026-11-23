"""Session entry declarations and the native log writer."""

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from uuid import uuid4

from core import attribution, entries, replay
from core.entries import Entry, EntrySpec, Link
from core.timeline import LOG_FORMAT, LogRecord, branch_note


def user_text(entry):
    sent = replay.sent(entry)
    if entry.get("mid_turn") and entry.get("source") != "mcp":
        return f"{replay.MID_TURN}\n{sent}"
    return sent


def reply_text(entry):
    return entries.combined_text(entry, origin="model")


def declare(type, role, *traits, project=entries.plain, public=(),
            fields_schema=None, required_fields=()):
    return EntrySpec(type, frozenset(traits), project=project, role=role,
                     public_fields=public, fields_schema=fields_schema,
                     required_fields=required_fields)


MESSAGE_FIELDS = ("text", "source", "inbox", "steer", "mid_turn", "continue",
                  "reference", "interaction")


ENTRY_SPECS = (
    declare("chat/user_message", "user_message", "user_message", "model_visible",
            "searchable", "durable_boundary", project=user_text, public=MESSAGE_FIELDS),
    declare("chat/assistant_reply", "assistant_reply", "assistant_reply",
            "model_visible", "searchable", project=reply_text, fields_schema={},
            public=("step", "finish_reason", "prompt_tokens", "cached_tokens",
                    "completion_tokens", "retries", "model_seconds", "empty", "reply",
                    "fallback", "interaction")),
    declare("chat/text", "text", "text", "model_visible", project=entries.text,
            public=("text",), fields_schema={"text": str}, required_fields=("text",)),
    declare("chat/reasoning", "reasoning", "reasoning", "model_visible",
            project=entries.text, public=("text",), required_fields=("text",),
            fields_schema={"text": str, "route": (str, type(None))}),
    declare("tools/call", "tool_call", "tool_call", "model_visible", "executable",
            public=("call_id", "name", "arguments"),
            fields_schema={"call_id": str, "name": str, "arguments": str},
            required_fields=("name", "arguments")),
    declare("tools/result", "tool_result", "tool_result", "model_visible",
            "searchable", "durable_boundary", project=lambda node: node.get("result"),
            public=("step", "call", "tool", "arguments", "result", "ran", "denied",
                    "bad_call", "failed", "saved_path", "output_at", "called_through",
                    "fields", "interaction")),
    declare("sessions/header", "header", "header", "durable_boundary",
            public=("format", "model", "mode", "note", "forked_from")),
    declare("sessions/system_prompt", "system_prompt", "system_prompt",
            "model_visible", project=entries.text),
    declare("sessions/notice", "notice", "notice", "model_visible",
            "searchable", "durable_boundary", project=replay.sent,
            public=(*MESSAGE_FIELDS, "summary", "left_behind", "started_from_seq")),
    declare("sessions/fork_note", "fork", "fork", "note", "model_visible",
            project=branch_note, public=("text", "kind", "forked_from")),
    declare("sessions/rewind_note", "rewind", "rewind", "model_visible",
            "durable_boundary", project=branch_note,
            public=("text", "left_from", "left_to")),
    declare("sessions/switch_note", "switch", "switch", "model_visible",
            project=entries.text,
            public=("text", "kind", "from", "to", "model_failure")),
    declare("sessions/harness_note", "note", "note", "model_visible",
            project=entries.text,
            public=("text", "kind", "model_failure", "record_type")),
    declare("sessions/ask", "ask", "ask", "searchable", "durable_boundary",
            project=lambda node: node.get("command", ""),
            public=("ask", "step", "call", "kind", "tool", "command", "why", "reason",
                    "about", "choices", "choice_labels", "interaction")),
    declare("sessions/answer", "answer", "answer", "model_visible", "searchable",
            "durable_boundary", project=user_text,
            public=(*MESSAGE_FIELDS, "ask", "call", "kind", "choice", "question_text",
                    "yes")),
    declare("sessions/step", "step", "step", public=("step", "seconds")),
    declare("sessions/turn_end", "turn_end", "turn_end", "durable_boundary",
            public=("inbox", "outcome", "answer", "reply", "detail", "error", "seconds",
                    "agent_seconds", "wait_seconds", "model_failure")),
    declare("sessions/retry", "retry", "retry",
            public=("step", "retry", "delay", "error", "model_failure")),
)

ROLE_BY_RECORD = {
    "session": "header", "system": "system_prompt", "user": "user_message",
    "assistant": "assistant_reply", "tool": "tool_result", "ask": "ask",
    "answer": "answer", "note": "note", "rewind": "rewind", "switch": "switch",
    "step": "step", "turn_end": "turn_end", "retry": "retry",
}


def build_record(seq, kind, parent=None, *, time=None, fields=None, session=None,
                 entry_id=None):
    """Build the timeline entry and its bookkeeping at the writer's boundary."""
    if isinstance(kind, Entry):
        if fields:
            raise ValueError("an entry already carries its fields")
        entry = kind.require_root()
    else:
        stamp = time or datetime.now().astimezone().isoformat(timespec="seconds")
        entry = build_entry(kind, stamp, dict(fields or {}), parent, entry_id)
        if session is not None:
            entry = attribution.logged_interaction(entry, session)
    if parent is not None and (type(parent) is not int or not 0 <= parent < seq):
        raise ValueError("a log record's parent must precede it")
    return LogRecord(seq, entry, parent, LOG_FORMAT if seq == 1 else None)


def build_entry(kind, time, fields, parent, entry_id):
    origin = {"user": "user", "assistant": "model", "tool": "tool",
              "answer": "user"}.get(kind, "system")
    role = ROLE_BY_RECORD.get(kind)
    if role is None and "/" not in kind:
        role = "note"
        fields["record_type"] = kind
    supplied = fields.pop("entry", None)
    children = tuple(fields.pop("entries", ()) or ())
    links = []
    if kind == "user" and (attribution.is_notice(fields) or fields.get("continue")):
        origin, role = "system", "notice"
        if fields.get("summary"):
            evidence = entries.make("text", origin="tool",
                                    fields={"text": fields.pop("text")})
            fields["text"] = fields["summary"]
            children = (*children, evidence)
    if kind == "note":
        role = {"forked": "fork", "switch": "switch"}.get(fields.get("kind"), role)
    if kind == "session" and "system_prompt" in fields:
        prompt = entries.make("system_prompt",
                              fields={"text": fields.pop("system_prompt")})
        children = (*children, prompt)
    elif kind == "system":
        fields["text"] = fields.pop("system_prompt")
    if kind == "tool" and (target := fields.get("call")):
        links.append(Link("answers", target))
    if kind == "ask" and (target := fields.get("call")):
        links.append(Link("asks_about", target))
    if kind == "answer" and (target := fields.get("ask")):
        links.append(Link("answers", target))
    if kind in ("session", "note") and (fork := fields.get("forked_from")):
        if target := fork.get("session"):
            links.append(Link("forked_from", target))
    if kind == "rewind" and parent is not None:
        target = fields.pop("parent_entry_id", None) or f"record-{parent}"
        links.append(Link("rewinds_to", target))
    if supplied is not None:
        return replace(
            supplied,
            id=supplied.id or entry_id or uuid4().hex,
            origin=supplied.origin or origin,
            created_at=supplied.created_at or time,
            fields={**(supplied.fields or {}), **fields},
            entries=(*(supplied.entries or ()), *children)
            if children else supplied.entries,
        )
    envelope = {
        "id": entry_id or (fields.get("ask") if kind == "ask" else None),
        "origin": origin, "created_at": time, "links": tuple(links) or None,
        "fields": fields or None, "entries": children or None,
    }
    envelope["id"] = envelope["id"] or uuid4().hex
    return entries.make(role, **envelope) if role else Entry(kind, **envelope)


class _LogEncoder(json.JSONEncoder):
    """Write frozen fields and nested entries directly as JSON."""

    def default(self, value):
        if isinstance(value, Entry):
            envelope = {
                name: item for name in entries.ENVELOPE
                if (item := getattr(value, name)) is not None or name in value._nulls
            }
            if value.extra:
                envelope.update(value.extra)
            return envelope
        if isinstance(value, Link):
            return value.to_dict()
        if isinstance(value, Mapping):
            return dict(value)
        return super().default(value)


_log_encoder = _LogEncoder(ensure_ascii=False)


def encode_record(record: LogRecord):
    """The UTF-8 bytes of a complete format 8 line."""
    line = {"seq": record.seq, "entry": record.entry.require_root()}
    if record.seq == 1:
        line.update(type="session", format=LOG_FORMAT)
    if record.parent is not None:
        line["parent"] = record.parent
    return (_log_encoder.encode(line) + "\n").encode()


def to_line(record: LogRecord):
    """A writable JSON object for exports or fixtures that edit the line."""
    return json.loads(encode_record(record))


def fork_header(record: LogRecord, last_entry, forked_from, row):
    """The copied header with its new row and its link to the fork point."""
    entry = record.entry
    links = tuple(link for link in entry.links or () if link.relation != "forked_from")
    entry = replace(entry, fields={**(entry.fields or {}), "forked_from": forked_from,
                                   "row": row},
                    links=(*links, Link("forked_from", last_entry.id)))
    return replace(record, entry=entry)
