"""Entries declared by the context component."""

from core import entries, replay
from core.entries import Entry, EntrySpec

HANDOFF = (
    "[Compacted {when}: the conversation before this point is replaced by "
    "this handoff, which you wrote. The session's log keeps all of it, and "
    "the outputs cleared from view are saved whole in the files named.]"
)
COMPACTED = "[Compacted {when}: {what}{size}{asked}]"
ASKERS = {"user": "The user", "zero": "You"}
ASKED = "\n{whose} asked for it, with these words on what the handoff keeps:\n{words}"
CLEARED_ONLY = ("older outputs were saved whole and cleared from view, each in "
                "the file its line names.")
HANDED_OFF = "the conversation before the kept steps became the handoff above."
FRESH = "the conversation before this point became the handoff above."
ON_OCCASION = "{occasion}, and {fresh}"
SIZE = " The context is now about {tokens:,} tokens of the {line:,} it is kept under."


def compacted(entry: Entry):
    """What a compaction did and the size it left, at the time it happened."""
    what = CLEARED_ONLY if entry.get("cleared_only") else HANDED_OFF
    if entry.get("fresh"):
        what = FRESH
        if occasion := entry.get("occasion"):
            what = ON_OCCASION.format(occasion=occasion, fresh=FRESH)
    size = ""
    if entry.get("tokens_after") and entry.get("line"):
        size = SIZE.format(tokens=entry.get("tokens_after"), line=entry.get("line"))
    asked = "".join(
        ASKED.format(whose=ASKERS.get(whose, whose), words=replay.quoted(words))
        for whose, words in (entry.get("instructions") or {}).items()
    )
    return COMPACTED.format(when=replay.when(entry.created_at), what=what, size=size,
                            asked=asked)


def handoff(entry: Entry):
    summary = replay.boundary_summary(entry)
    if not summary:
        return ""
    stamp = entry.get("handoff_at") or entry.created_at
    if stamp is None:
        return summary
    head = HANDOFF.format(when=replay.when(stamp))
    return f"{head}\n\n{summary}"


def compaction_notice_for(entry: Entry):
    """Project a compaction handoff as a notice at its timeline position."""
    if entry.get("kept_from") is None:
        return None
    return entries.make(
        "compaction_notice", id=f"{entry.id}-notice", origin=entry.origin,
        created_at=entry.created_at, fields=entry.fields,
    )


ENTRY_SPECS = (
    EntrySpec("context/compaction_handoff", frozenset({
        "model_visible", "context_boundary", "compaction_handoff",
        "searchable", "durable_boundary",
    }), project=handoff, role="compaction_handoff",
              public_fields=("summary", "handoff_at", "reason", "line", "tokens_before",
                             "tokens_after", "instructions", "attachments",
                             "interaction"),
              replay_notice=compaction_notice_for),
    EntrySpec("context/compaction_notice", frozenset({
        "model_visible", "compaction_notice", "note",
    }), project=compacted, role="compaction_notice",
              public_fields=("reason", "line", "tokens_before", "tokens_after")),
    EntrySpec("context/compaction_failed", frozenset({"compaction_failed"}),
              role="compaction_failed",
              public_fields=("reason", "error", "model_failure")),
    EntrySpec("context/nothing_to_compact", frozenset({"nothing_to_compact"}),
              role="nothing_to_compact", public_fields=("reason",)),
)
