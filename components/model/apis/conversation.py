"""The entries model adapters can project without endpoint-specific data."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from threading import Lock
from types import MappingProxyType

from components.attachments.entries import attachment_nodes, project_attachment
from core import entries, replay
from core.llm import tool_entries

WIRE_ROLES = {"user": "user", "system": "system", "model": "assistant"}
ASSISTANT_PARTS = ("text", "reasoning")
EMPTY_TOOL_ATTACHMENTS = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class AttachmentDescriptor:
    """Neutral attachment metadata cached per Entry (never bytes or decisions)."""

    attachment_id: str
    name: str
    media_type: str
    size_bytes: int | None = None
    sha256: str | None = None
    delivery: str = "inline"
    path: str | None = None
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    pages: int | None = None
    origin: str = "user"
    reference_text: str = ""

    def reference(self, reason=None):
        """Compact neutral reference text, optionally noting why native send stopped."""
        if not reason:
            return self.reference_text
        return project_attachment(
            {
                "attachment_id": self.attachment_id,
                "name": self.name,
                "media_type": self.media_type,
                "size_bytes": self.size_bytes,
                "path": self.path,
                "width": self.width,
                "height": self.height,
                "duration_seconds": self.duration_seconds,
                "pages": self.pages,
            },
            reason=reason,
        )


def descriptor_from_entry(node, *, origin="user"):
    """Extract a neutral AttachmentDescriptor from an attachments/file entry."""
    size = node.get("size_bytes")
    width = node.get("width")
    height = node.get("height")
    duration = node.get("duration_seconds")
    pages = node.get("pages")
    return AttachmentDescriptor(
        attachment_id=str(node.get("attachment_id") or ""),
        name=str(node.get("name") or node.get("attachment_id") or "file"),
        media_type=str(node.get("media_type") or "application/octet-stream"),
        size_bytes=size if type(size) is int and size >= 0 else None,
        sha256=node.get("sha256") if isinstance(node.get("sha256"), str) else None,
        delivery=str(node.get("delivery") or "inline"),
        path=str(node.get("path")) if node.get("path") else None,
        width=width if type(width) is int and width > 0 else None,
        height=height if type(height) is int and height > 0 else None,
        duration_seconds=(
            float(duration)
            if isinstance(duration, (int, float)) and not isinstance(duration, bool)
            else None
        ),
        pages=pages if type(pages) is int and pages > 0 else None,
        origin=origin,
        reference_text=project_attachment(node),
    )


def visible(node):
    return entries.has(node, "model_visible") and not (
        entries.has(node, "tool_call")
        or entries.has(node, "tool_result")
        or entries.has(node, "attachment")
    )


def passages(entry, *, assistant=False):
    """Neutral text and its origin for entries without a native wire item."""
    nodes = tuple(replay.model_nodes(entry, stop_at="tool_result"))
    evidence = []
    if entries.has(entry, "notice"):
        evidence = [node for node, origin, _, _ in nodes
                    if origin == "tool" and visible(node)]
    if evidence:
        summary = entry.get("summary") or entries.text(entry)
        parts = [summary]
        for node in evidence:
            if text := entries.project(node):
                if summary and text.startswith(summary):
                    parts.append(text[len(summary):])
                else:
                    parts.append(f"\n\n[Tool evidence]\n{text}")
        yield "user", replay.sent(entry, text="".join(parts))
    handoff = entries.has(entry, "context_boundary")
    for node, origin, created_at, _ in nodes:
        if not visible(node) or (evidence and (node is entry or origin == "tool")):
            continue
        if entries.has(node, "assistant_reply"):
            continue
        if assistant and origin == "model" and any(
            entries.has(node, trait) for trait in ASSISTANT_PARTS
        ):
            continue
        if node.created_at is None and created_at is not None:
            node = replace(node, created_at=created_at)
        text = entries.project(node)
        if text:
            if origin == "tool":
                text = f"[Tool evidence]\n{text}"
            role = WIRE_ROLES.get(origin, "user")
            if handoff and node is entry:
                role = "user"
            yield role, text


@dataclass(frozen=True, slots=True)
class Projection:
    assistant: bool
    content: str
    passages: tuple
    tools: tuple
    attachments: tuple = ()
    tool_attachments: Mapping = EMPTY_TOOL_ATTACHMENTS


def projection(entry):
    assistant = entry.origin == "model" and entries.has(entry, "assistant_reply")
    entry_tools = tool_entries(entry)
    direct = ()
    if not entries.has(entry, "tool_result"):
        origin = entry.origin or "user"
        direct = tuple(
            descriptor_from_entry(child, origin=origin)
            for child in attachment_nodes(entry)
        )
    by_result = {}
    for result in entry_tools[1]:
        found = attachment_nodes(result)
        if found:
            by_result[id(result)] = tuple(
                descriptor_from_entry(child, origin="tool") for child in found
            )
    tool_atts = MappingProxyType(by_result) if by_result else EMPTY_TOOL_ATTACHMENTS
    return Projection(
        assistant,
        entries.project(entry) if assistant else "",
        tuple(passages(entry, assistant=assistant)),
        entry_tools,
        direct,
        tool_atts,
    )


def project_entries(conversation):
    return {id(entry): projection(entry) for entry in conversation}


class ProjectionCache:
    """Reuse neutral projections for immutable entries in successive requests."""

    def __init__(self):
        self.lock = Lock()
        self.previous = {}

    def project(self, conversation):
        with self.lock:
            current = {}
            for entry in conversation:
                key = id(entry)
                saved = self.previous.get(key)
                if saved is None:
                    saved = entry, projection(entry)
                current[key] = saved
            self.previous = current
            return {key: saved[1] for key, saved in current.items()}


def legacy_provider_items(state, kind):
    if not isinstance(state, Mapping):
        return
    if kind in ("response_item", "reasoning_detail"):
        key = "items" if kind == "response_item" else "reasoning_details"
        for item in state.get(key) or ():
            yield {"item": item}
    if kind == "thought_signature":
        for call_id, signature in (state.get("thought_signatures") or {}).items():
            if isinstance(signature, Mapping) and "extra_content" in signature:
                yield {**signature, "call_id": call_id}


def provider_items(entry, route, kind):
    if route is None:
        return
    for node in entries.matching(entry, "private_continuation"):
        if node.get("route") != route:
            continue
        if node.get("kind") == kind:
            yield node.fields
        elif node.get("kind") == "legacy_provider_state":
            yield from legacy_provider_items(node.get("item"), kind)


def reasoning(entry, route):
    return "\n\n".join(
        entries.project(node)
        for node, origin, _, _ in entries.walk(entry, include_private=False)
        if origin == "model" and entries.has(node, "reasoning")
        and node.get("route") in (None, route)
    )
