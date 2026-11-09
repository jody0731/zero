"""How big the context is, and how much room it has.

A call's size is known after it: the server counts the prompt's tokens.
What came since (the reply, the results, the user's next message) is
counted by the server where it can (llama.cpp's /tokenize), else taken as a
token for every byte of UTF-8. A byte-level tokenizer gives no byte two
tokens, so that never counts low: digits, hex and rare characters come to a
token a byte, where a token every three bytes counted them a third (LT-21's
tokenizer, measured on 23 kinds of text, 8 Nov). It counts English three or
four times high, until the next call counts it.

The line is `compact_at_tokens`, or the model's window when it is unset, and
never past the window. The headroom is what must fit beside the context: the
next reply (`reply_room_tokens`), and a compaction's instructions and handoff
(`handoff_room_tokens`). A result ZERO didn't size gets a share
(`result_share_percent`) of the room left below the line minus the
headroom; past it, the whole goes to a file (hooks.py). Each is read from a
session's limits as they stand (settings.LiveSettings).
"""

import json
from dataclasses import dataclass

from core import history

BYTES_PER_TOKEN = 1


def tokens(text):
    """`text`'s tokens with no count from the server: one a byte, never
    fewer than the server's."""
    return -(-len(text.encode("utf-8")) // BYTES_PER_TOKEN)


def message_tokens(message, count=tokens):
    """A message's tokens, as it is sent; `count`: text's tokens (the
    server's count where there is one)."""
    return count(json.dumps(message, ensure_ascii=False))


@dataclass(frozen=True)
class Size:
    """The context's tokens, its system message and tools included, and how
    many of them are estimated rather than counted by the server."""

    tokens: int
    estimated: int


def measure(records, messages, schemas, tokens=tokens):
    """The context's size: the last call's count plus what came after it
    (`tokens`: text's tokens, the server's count where there is one), when
    that call saw the conversation as it stands; else all of it so (no call
    yet, or a model switch or a compaction since). New code since that call
    rewrote the system message and the tools around the same conversation
    on the same model: the count holds, and those two are added whole, so
    their old copies are counted twice, never low."""
    last, rewritten = None, False
    for index, record in enumerate(records):
        if record["type"] == "system" and record.get("why") == "code":
            rewritten = last is not None
        elif record["type"] in history.SYSTEM_RECORDS:
            last = None
        elif record["type"] == "assistant" and record.get("prompt_tokens"):
            last, rewritten = index, False
    if last is not None:
        after = [found for record in records[last:]
                 if (found := history.message(record)) is not None]
        guessed = sum(message_tokens(m, tokens) for m in after)
        if rewritten:
            guessed += sum(message_tokens(m, tokens) for m in messages
                           if m["role"] == "system")
            guessed += tokens(json.dumps(schemas, ensure_ascii=False))
        return Size(records[last]["prompt_tokens"] + guessed, guessed)
    guessed = sum(message_tokens(m, tokens) for m in messages)
    guessed += tokens(json.dumps(schemas, ensure_ascii=False))
    return Size(guessed, guessed)


def line(limits, window):
    """The tokens a session's context is kept under: its compact_at_tokens,
    or the model's window when unset, and never past the window."""
    chosen = limits.get("compact_at_tokens") or window
    return min(chosen, window)


def reply_room(at, limits):
    """The next reply's room under the line `at`: reply_room_tokens, at most
    an eighth of the line, so a small window keeps room for the talk."""
    return min(limits["reply_room_tokens"], at // 8)


def handoff_room(at, limits):
    """A compaction handoff's room under the line `at`, its ceiling too:
    handoff_room_tokens, at most an eighth of the line."""
    return min(limits["handoff_room_tokens"], at // 8)


def headroom(at, instructions, limits):
    """What must fit beside the context under the line `at`: the next reply,
    a compaction's `instructions` and its handoff. No margin: what the
    server didn't count is counted high."""
    return reply_room(at, limits) + handoff_room(at, limits) + instructions


def room_left(at, size, instructions, limits):
    """The tokens left before the context must be compacted; below zero, it
    must be now."""
    return at - headroom(at, instructions, limits) - size.tokens


def share(room, limits):
    """What a result ZERO didn't size may take of the room left:
    result_share_percent of it."""
    return max(room, 0) * limits["result_share_percent"] // 100


def fresh_room(at, fixed, instructions, limits):
    """The most one result can be: what a fresh context has, after its
    system message and tools (`fixed` tokens) and a handoff, below the line
    and the headroom."""
    return (at - headroom(at, instructions, limits) - fixed
            - handoff_room(at, limits))
