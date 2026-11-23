"""The entries model adapters can project without endpoint-specific data."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from threading import Lock

from core import entries, replay
from core.llm import tool_entries

WIRE_ROLES = {"user": "user", "system": "system", "model": "assistant"}
ASSISTANT_PARTS = ("text", "reasoning")


def visible(node):
    return entries.has(node, "model_visible") and not (
        entries.has(node, "tool_call") or entries.has(node, "tool_result")
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


def projection(entry):
    assistant = entry.origin == "model" and entries.has(entry, "assistant_reply")
    return Projection(assistant, entries.project(entry) if assistant else "",
                      tuple(passages(entry, assistant=assistant)), tool_entries(entry))


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
