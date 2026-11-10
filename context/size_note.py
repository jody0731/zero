"""The context's size told to ZERO each time it has grown by a mark
(`context_note_every_tokens`; 0: never): how big it is, what a step counts
for reading it again, and what this turn has used so far.

A fact that comes to ZERO at a moment: on a large window no compaction
comes, so nothing else in a turn says it has grown (two real turns passed
270,000 tokens unremarked, half their weighted tokens spent reading the
conversation again); and `usage` holds the same numbers only for a ZERO
that asks. Numbers alone: what to do about them, if anything, is ZERO's
pick (principle 3), and the note names no means.

The size is the last call's own count (usage/spend.py, `context_tokens`),
not room.py's measure, which adds an estimate of what came since and
counts English three or four times high: a note is written before the
step after the one that passed the mark. The marks start again when the
context is written whole (a compaction, a round's fresh start, a model
switch). The weights are usage/'s, as every weighted token is.
"""

from core import history
from core.hooks import Hook
from usage.spend import (
    context_tokens, reread_weighted_tokens, tokens_text,
    turn_reread_weighted_tokens, turn_weighted_tokens,
)

GROWN = (
    "[Harness note: this session's context has reached {tokens:,} tokens. "
    "Each step reads it again: {step} a step at this size.{turn}]"
)
# What the turn under way has used, where it has made a call
TURN_SO_FAR = (
    " This turn's {steps} used {used}, {reread} of them reading the "
    "conversation again."
)


def noted_marks(records, every):
    """How many marks of `every` tokens the context's last note stood past,
    in the context as it stands: 0 with none since it was last written
    whole (usage/spend.py's `context_tokens` says when that is)."""
    marks = 0
    for record in records:
        if record["type"] in history.SYSTEM_RECORDS:
            if not (record["type"] == "system" and record.get("why") == "code"):
                marks = 0
        elif record["type"] == "note" and record.get("kind") == "context_size":
            marks = record["tokens"] // every
    return marks


def turn_so_far(records, price, fallback_price):
    """The open turn's use as the note says it; "" before its first call."""
    steps = sum(r["type"] == "assistant" for r in records)
    if not steps:
        return ""
    reread = turn_reread_weighted_tokens(records, price, fallback_price)
    return TURN_SO_FAR.format(
        steps="1 step has" if steps == 1 else f"{steps:,} steps have",
        used=tokens_text(turn_weighted_tokens(records, price, fallback_price)),
        reread=f"{round(reread):,}")


class SizeNote(Hook):
    """One note each time the context has grown by the mark. `limits`: the
    session's, read at each step."""

    def __init__(self, limits):
        self.limits = limits

    def before_model(self, step):
        every = self.limits["context_note_every_tokens"]
        if not every:
            return None
        session = step.turn.session
        records = session.log.branch()
        tokens = context_tokens(records)
        if tokens is None or tokens // every <= noted_marks(records, every):
            return None
        price = session.settings.get("price")
        turn = turn_so_far(history.unfinished_turn(records), price,
                           session.settings.get("fallback_price"))
        session.write("note", kind="context_size", tokens=tokens, text=GROWN.format(
            tokens=tokens, turn=turn,
            step=tokens_text(reread_weighted_tokens(tokens, price))))
        return None
