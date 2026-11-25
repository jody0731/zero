"""Model context and transcript projections of a native conversation timeline."""

import json
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

from core import attribution, entries, timeline
from core.entries import Entry
from core.timeline import LogRecord
from core.timeline import OLD_BRANCH_EXCERPTS as OLD_BRANCH_EXCERPTS
from core.timeline import REWOUND as REWOUND
from core.timeline import without_question_echoes as without_question_echoes

SYSTEM_RECORDS = ("header", "system_prompt", "context_boundary")
UNKNOWN = (
    "Tool call interrupted before a result was recorded; its outcome is unknown."
)
MID_TURN = "[The user wrote this while you were working.]"
SENT = "[Sent {when}]"
NOTICE = '<notice from="{source}">\n{text}\n</notice>'
# A notice can quote arbitrary output, including something that looks like its end.
CLOSING_TAG = re.compile(r"<(/\s*notice)", re.IGNORECASE)
LEFT_BEHIND = (
    "[This started from record {seq} of this session's log, on a part of the "
    "conversation a rewind left behind.]"
)


def when(iso):
    """Format the local time a message was sent for the model."""
    moment = datetime.fromisoformat(iso).astimezone()
    return f"{moment:%a} {moment.day} {moment:%b %Y, %H:%M %Z}"


def sent(entry: Entry, *, text=None):
    """A message's text and timestamp, with attribution around a notice."""
    text = entries.text(entry) if text is None else text
    source = entry.get("source")
    if attribution.is_notice(entry):
        if entry.get("left_behind"):
            note = LEFT_BEHIND.format(seq=entry.get("started_from_seq"))
            text = f"{note}\n{text}"
        text = NOTICE.format(source=source, text=CLOSING_TAG.sub(r"<\\\1", text))
    elif source == "mcp":
        text = f"[Message from {source}]\n{text}"
    stamp = entry.created_at
    return f"{text}\n\n{SENT.format(when=when(stamp))}" if stamp else text


def quoted(text):
    """Mark each line of text as a quotation."""
    return "\n".join(f"> {line}" for line in text.strip().splitlines())


def starts_context(entry: Entry):
    """Whether a record can carry the system prompt that starts model context."""
    return any(entries.has(entry, trait) for trait in SYSTEM_RECORDS)


def system_entry(record: LogRecord):
    """The system prompt, promoted to its own root when nested."""
    root = record.entry
    for node, origin, stamp, _ in entries.walk(root, include_private=False):
        if origin == "system" and entries.has(node, "system_prompt"):
            if node is root:
                return node
            return replace(node, id=node.id or root.id, origin=origin, created_at=stamp)
    return None


def reply_has_output(entry: Entry):
    """Whether an assistant reply carries text, calls, hosted output, or media."""
    for node, origin, _, depth in entries.walk(entry, include_private=False):
        if depth == 0:
            continue
        if origin == "model" and entries.has(node, "tool_call"):
            return True
        if entries.has(node, "text") and (entries.text(node) or "").strip():
            return True
        if entries.has(node, "hosted_output"):
            return True
        if entries.has(node, "attachment") or entries.has(node, "media"):
            return True
    return False


def model_entry(record: LogRecord):
    """The entry the model reads as part of the timeline."""
    entry = record.entry
    if entries.has(entry, "system_prompt") or entries.has(entry, "header"):
        return None
    if (notice := entries.replay_notice(entry)) is not None:
        return notice
    if entries.has(entry, "answer") and not entry.get("as_message"):
        return None
    if (entries.has(entry, "assistant_reply") and entry.get("empty")
            and not reply_has_output(entry)):
        return None
    if (entries.has(entry, "note") or entries.has(entry, "switch")) and (
        not entry.get("text")
    ):
        return None
    return entry if entries.has(entry, "model_visible") else None


def calls(record: LogRecord):
    return entries.tool_calls(record.entry)


def result_target(record: LogRecord):
    return entries.linked_to(record.entry, "answers")


def _downgrade_attachment(node: Entry):
    """Downgrade inline attachment children to reference delivery when cleared."""
    if entries.has(node, "attachment") and node.get("delivery") != "reference":
        return replace(node, fields={**(node.fields or {}), "delivery": "reference"})
    if node.entries:
        updated = tuple(_downgrade_attachment(child) for child in node.entries)
        if any(a is not b for a, b in zip(updated, node.entries)):
            return replace(node, entries=updated)
    return node


def with_result(entry: Entry, text, *, clear_media=True):
    """Keep a result's envelope and children when its text moves to a file.

    When `clear_media` is True, inline attachment children are downgraded to
    `delivery="reference"` so stale tool media is not re-sent natively.
    """
    fields = {**(entry.fields or {}), "result": text} if text is not None else (
        entry.fields
    )
    children = entry.entries
    if clear_media and children:
        updated = tuple(_downgrade_attachment(child) for child in children)
        if any(a is not b for a, b in zip(updated, children)):
            children = updated
    return replace(entry, fields=fields, entries=children)


def boundary_summary(entry: Entry):
    """The model-written summary carried by a context boundary entry."""
    return entry.get("summary") or entries.combined_text(entry, origin="model")


def latest_context_boundary(records: Sequence[LogRecord]):
    """The latest context boundary that sets the cutoff of the context."""
    for record in reversed(records):
        entry = record.entry
        if entries.has(entry, "context_boundary") and "kept_from" in (
            entry.fields or {}
        ):
            return record
    return None


def system_now(records: Sequence[LogRecord]):
    """The latest record that carries a system message."""
    for record in reversed(records):
        if starts_context(record.entry) and system_entry(record) is not None:
            return record
    return None


def in_context(records: Sequence[LogRecord]):
    """The saved records the context keeps after its system prompt and boundary."""
    boundary = latest_context_boundary(records)
    kept = (boundary.entry.get("kept_from") or 0) if boundary else 0
    if boundary and kept:
        # An older compactor could cut at a notice inside a tool batch.
        pending = {}
        for record in records:
            if record.seq >= kept:
                break
            entry = record.entry
            if entries.has(entry, "assistant_reply"):
                for call in calls(record):
                    pair = (record.seq, call.id, entries.call_id(call))
                    pending.update((target, pair) for target in pair[1:] if target)
            elif entries.has(entry, "tool_result"):
                if pair := pending.get(result_target(record)):
                    for alias in pair[1:]:
                        pending.pop(alias, None)
        if pending:
            kept = min(pair[0] for pair in pending.values())
    return [
        record for record in without_question_echoes(records)
        if record.seq >= kept and not (
            entries.has(record.entry, "header")
            or entries.has(record.entry, "system_prompt")
        )
    ]


def handoff(boundary: LogRecord):
    """The saved context boundary if it carries a summary."""
    return boundary.entry if boundary_summary(boundary.entry) else None


def model_nodes(entry: Entry, *, stop_at=None):
    """Project a boundary once, with the system prompt replayed on its own."""
    nodes = entries.walk(entry, include_private=False, stop_at=stop_at)
    if not entries.has(entry, "context_boundary"):
        return nodes
    return (
        item for item in nodes
        if not (entries.has(item[0], "system_prompt")
                or (item[1] == "model" and entries.has(item[0], "text")))
    )


def _tool_text(entry: Entry):
    output = entries.project(entry)
    if output is None or isinstance(output, str):
        return output
    return json.dumps(entries.thaw(output), ensure_ascii=False)


def tool_output(entry: Entry, *, include_attachments=True):
    """A tool's native output and the visible text supplied in its children."""
    output = _tool_text(entry)
    if not entry.entries:
        return output
    evidence = tuple(
        text
        for node, origin, _, depth in model_nodes(entry)
        if depth and origin == "tool" and entries.has(node, "model_visible")
        and not entries.has(node, "tool_call")
        and (include_attachments or not entries.has(node, "attachment"))
        and (text := _tool_text(node))
    )
    return "\n\n".join(filter(None, (output, *evidence))) if evidence else output


def transcript(records: Sequence[LogRecord]):
    """Old reply echoes can follow tool results, even across a page boundary."""
    answers = set()
    for record in records:
        entry = record.entry
        inbox = entry.get("inbox")
        if entries.has(entry, "answer") and inbox:
            answers.add(inbox)
        if attribution.is_turn_input(entry) and entry.get("steer") and inbox in answers:
            continue
        yield record


def replay(records: Sequence[LogRecord], *, current_branch=False):
    """The active model context after its system prompt and context boundary."""
    if not records:
        return ()
    if not current_branch:
        records = timeline.branch(records)
    system = system_now(records)
    prompt = system_entry(system) if system is not None else None
    conversation = [prompt] if prompt is not None else []
    boundary = latest_context_boundary(records)
    cleared = {}
    if boundary is not None:
        cleared = boundary.entry.get("cleared") or {}
        if start := handoff(boundary):
            conversation.append(start)
    for record in in_context(records):
        found = model_entry(record)
        if found is None:
            continue
        if entries.has(found, "tool_result") and str(record.seq) in cleared:
            found = with_result(found, cleared[str(record.seq)])
        conversation.append(found)
    return tuple(conversation)
