"""Pure-dict helpers for reading Protocol 8 native `Entry` wire records.

Zero imports so `protocol` stays at the bottom of the dependency graph
(design rule 1, `test_layers.py`). Every app and client helper reads timeline
records (`{"seq", "time", "entry", ...}`) through these functions.
"""

COMPACTION_TYPES = (
    "context/compaction_handoff",
    "context/compaction_failed",
    "context/nothing_to_compact",
)
NOTE_TYPES = (
    "sessions/harness_note",
    "sessions/fork_note",
    "context/compaction_notice",
)


def entry_of(record):
    """The `Entry` dict of a wire record, or the dict itself if already an entry."""
    if not isinstance(record, dict):
        return {}
    entry = record.get("entry")
    if isinstance(entry, dict):
        return entry
    return record if isinstance(record.get("type"), str) else {}


def entry_type(record):
    """The namespaced entry type (`owner/name`), or None."""
    return entry_of(record).get("type")


def fields_of(record):
    """The entry's `fields` mapping (never None)."""
    fields = entry_of(record).get("fields")
    return fields if isinstance(fields, dict) else {}


def field(record, name, default=None):
    """One field from the record's entry, or `default`."""
    return fields_of(record).get(name, default)


def links_of(record):
    """The entry's `links` list (empty tuple when absent)."""
    links = entry_of(record).get("links")
    return links if isinstance(links, (list, tuple)) else ()


def linked_to(record, relation):
    """The first `target_id` linked with `relation`, or None."""
    for link in links_of(record):
        if isinstance(link, dict) and link.get("relation") == relation:
            return link.get("target_id")
    return None


def children_of(record):
    """The entry's direct child entries (empty tuple when absent)."""
    children = entry_of(record).get("entries")
    return children if isinstance(children, (list, tuple)) else ()


def walk_entry(entry, origin=None):
    """Yield `(node, effective_origin)` recursively over a dict entry."""
    if not isinstance(entry, dict):
        return
    effective = entry.get("origin") or origin
    yield entry, effective
    children = entry.get("entries")
    if isinstance(children, (list, tuple)):
        for child in children:
            yield from walk_entry(child, effective)


def combined_text(record, kind="chat/text", origin=None):
    """Join text from matching nested entries in envelope order."""
    parts = []
    for node, effective in walk_entry(entry_of(record)):
        if node.get("type") != kind:
            continue
        if origin is not None and effective != origin:
            continue
        fields = node.get("fields")
        text = fields.get("text") if isinstance(fields, dict) else None
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def assistant_text(record):
    """The model's visible reply text from a `chat/assistant_reply` record."""
    return combined_text(record, "chat/text", origin="model")


def assistant_reasoning(record):
    """The model's reasoning text from a `chat/assistant_reply` record."""
    return combined_text(record, "chat/reasoning", origin="model")


def tool_calls(record):
    """All model-origin `tools/call` child entries in envelope order."""
    return [
        node for node, effective in walk_entry(entry_of(record))
        if node.get("type") == "tools/call" and effective == "model"
    ]


def call_id(call):
    """The call identifier of a `tools/call` entry dict."""
    if not isinstance(call, dict):
        return ""
    fields = call.get("fields")
    if isinstance(fields, dict) and fields.get("call_id"):
        return fields["call_id"]
    return call.get("id") or ""


def call_name(call):
    """The tool name of a `tools/call` entry dict."""
    fields = call.get("fields") if isinstance(call, dict) else None
    return fields.get("name", "") if isinstance(fields, dict) else ""


def call_arguments(call):
    """The raw JSON arguments string of a `tools/call` entry dict."""
    fields = call.get("fields") if isinstance(call, dict) else None
    return fields.get("arguments", "") if isinstance(fields, dict) else ""


def is_continue(record):
    """Whether `record` is a `/continue` turn-start notice."""
    return entry_type(record) == "sessions/notice" and bool(field(record, "continue"))


def is_notice(record):
    """Whether `record` is a background notice (not a `/continue` prompt)."""
    if is_continue(record):
        return False
    if isinstance(record, dict) and record.get("notice_source"):
        return True
    return entry_type(record) == "sessions/notice"


def is_turn_input(record):
    """Whether `record` is a user message or session notice delivered to a turn."""
    return entry_type(record) in ("chat/user_message", "sessions/notice")


def is_turn_start(record):
    """Whether `record` opens a new turn (non-steer user message or notice)."""
    return is_turn_input(record) and not field(record, "steer")


def message_text(record):
    """The full delivered text of a user message, notice, or answer.

    Job and tool notices move full output into a child `chat/text` with
    `origin == "tool"` while keeping the short summary in `fields.text`.
    """
    if entry_type(record) == "sessions/notice":
        for child in children_of(record):
            if isinstance(child, dict) and child.get("origin") == "tool":
                child_fields = child.get("fields")
                if isinstance(child_fields, dict) and "text" in child_fields:
                    return child_fields.get("text") or ""
    return field(record, "text", "") or ""


def compaction_summary(record):
    """The handoff summary from child model `chat/text` or `fields.summary`."""
    summary = combined_text(record, "chat/text", origin="model")
    if summary:
        return summary
    return field(record, "summary") or ""


def ask_id(record):
    """The ask id from `fields.ask`, `entry.id`, or an `answers` link."""
    kind = entry_type(record)
    if kind == "sessions/ask":
        return field(record, "ask") or entry_of(record).get("id")
    if kind == "sessions/answer":
        return field(record, "ask") or linked_to(record, "answers")
    return field(record, "ask")


def tool_call_id(record):
    """The tool call id from `fields.call`, an `answers` link, or `asks_about`."""
    kind = entry_type(record)
    if kind == "tools/result":
        return field(record, "call") or linked_to(record, "answers")
    if kind == "sessions/ask":
        return field(record, "call") or linked_to(record, "asks_about")
    return field(record, "call")


def interaction(record):
    """The attribution `interaction` object from the entry's fields."""
    value = field(record, "interaction")
    return value if isinstance(value, dict) else None


def model_failure(record):
    """The redacted `model_failure` dict from the entry's fields, or None."""
    value = field(record, "model_failure")
    return value if isinstance(value, dict) else None
