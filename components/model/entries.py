"""Turn streamed data into a reply and its private provider continuation."""

from collections.abc import Mapping

from core import entries
from core.entries import EntrySpec


def hosted_output_text(entry):
    """Portable summary of a completed provider-executed output."""
    if summary := entry.get("summary"):
        return summary
    kind = entry.get("kind") or "hosted_output"
    status = entry.get("status")
    return f"[Hosted {kind}: {status}]" if status else f"[Hosted {kind}]"


ENTRY_SPECS = (
    EntrySpec("model/provider_item", frozenset({"private_continuation"}),
              role="provider_item", required_fields=("route", "kind"), fields_schema={
                  "route": (str, type(None)), "kind": str, "item": object,
                  "call_id": str, "name": str, "arguments": str,
                  "extra_content": Mapping, "thought_signature": str,
                  "attachment_id": (str, type(None)),
              }),
    EntrySpec("model/hosted_output", frozenset({"hosted_output", "model_visible",
                                                "searchable"}),
              role="hosted_output",
              project=hosted_output_text,
              required_fields=("kind", "execution", "status"),
              public_fields=("kind", "execution", "status", "item_id", "call_id",
                             "summary", "output"),
              fields_schema={
                  "kind": str, "execution": str, "status": str,
                  "item_id": (str, type(None)), "call_id": (str, type(None)),
                  "route": (str, type(None)), "summary": (str, type(None)),
                  "output": object,
              }),
)


CONTINUATION_KINDS = (
    "response_item", "interaction_step", "thought_signature", "reasoning_detail",
)


def provider_items(draft, route, keep_continuation):
    if not keep_continuation:
        return
    known = [kind for kind in CONTINUATION_KINDS
             if kind in draft.continuation_items]
    extra = sorted(kind for kind in draft.continuation_items
                   if kind not in CONTINUATION_KINDS)
    for kind in (*known, *extra):
        for _, fields in sorted(draft.continuation_items[kind].items()):
            yield entries.make("provider_item",
                               fields={"route": route, "kind": kind, **fields})


def reply_from_stream(draft, *, route=None, keep_continuation=True):
    children = []
    if draft.reasoning:
        children.append(entries.make(
            "reasoning", fields={"text": draft.reasoning, "route": route},
        ))
    if draft.content:
        children.append(entries.make("text", fields={"text": draft.content}))
    for _, call in sorted(draft.calls.items()):
        fields = {key: value for key, value in call.items() if key != "id"}
        children.append(entries.make("tool_call", id=call["id"], fields=fields))
    for _, item in sorted(draft.hosted_outputs.items()):
        children.append(entries.make("hosted_output",
                                     fields={"route": route, **item}))
    for _, item in sorted(draft.unrecognized_outputs.items()):
        children.append(entries.Entry("model/unrecognized_output", fields=item))
    if route is not None:
        children.extend(provider_items(draft, route, keep_continuation))
    return entries.make("assistant_reply", origin="model", entries=tuple(children))
