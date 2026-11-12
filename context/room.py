"""How big the context is, and how much room it has.

A call's size is known after it: the server counts the prompt's tokens.
What came since (the reply, the results, the user's next message) is
counted by the server where it can (llama.cpp's /tokenize). Else, where the
server refuses a request longer than its context, it is estimated at the
bytes a token the session's own counted steps show (bytes_a_token): a guess
low there is caught by the refusal, one compaction and the call again.
Where a server may cut such a request silently, or before a step has
counted, a token for every byte of UTF-8, which never counts low: a
byte-level tokenizer gives no byte two tokens, and digits, hex and rare
characters come to a token a byte (measured on 23 kinds of text). It counts
English three or four times high, until the next call counts it.

The line is `compact_at_tokens`, or the model's window when it is unset, and
never past the window. The headroom is what must fit beside the context: the
next reply (`reply_room_tokens`), and a compaction's instructions and handoff
(`handoff_room_tokens`). A result ZERO didn't size gets a share
(`result_share_percent`) of the room left below the line minus the
headroom, or, near the line, of the room a compaction leaves, whichever is
more; past it, the whole goes to a file (hooks.py). Each is read from a
session's limits as they stand (settings.LiveSettings).
"""

import json
from dataclasses import dataclass

from core import history

BYTES_PER_TOKEN = 1
# The steps a session's pace, and its bytes a token, are measured over: its
# newest
PACE_STEPS = 20
# A step with less between its two calls tells too little of the bytes a
# token: a message's own wrapping counts for more there
LEAST_STEP_BYTES = 1_000


def tokens(text, bytes_a_token=BYTES_PER_TOKEN):
    """`text`'s tokens with no count from the server: its bytes over
    `bytes_a_token`, rounded up; at one a byte, never fewer than the
    server's."""
    return int(-(-len(text.encode("utf-8")) // bytes_a_token))


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


def fixed_tokens(records, messages, schemas, tokens=tokens):
    """The tokens every context holds, its system message and tools: their
    count (`tokens`, a token a byte where the server counts nothing), but
    never more than the smallest prompt a call on this model has sent, which
    held them and more. A token a byte counted a start of 6.4k tokens as
    25k, and a fresh context's room as below zero (7.3's eval)."""
    found = tokens(messages[0]["content"]) + tokens(
        json.dumps(schemas, ensure_ascii=False))
    for record in records:
        if record["type"] == "system" and record.get("why") == "model":
            # Another model's tokenizer: its own calls from here on
            found = tokens(messages[0]["content"]) + tokens(
                json.dumps(schemas, ensure_ascii=False))
        elif record["type"] == "assistant" and record.get("prompt_tokens"):
            found = min(found, record["prompt_tokens"])
    return found


def tokens_a_step(records):
    """The tokens a step adds to the context at the session's own pace: the
    mean growth of its server's count from one call to the next, over its
    newest PACE_STEPS steps, each pair of calls on one context (no
    compaction, model switch or new system message between); None before
    two calls have counted."""
    grown, last = [], None
    for record in records:
        if record["type"] in history.SYSTEM_RECORDS:
            last = None
        elif record["type"] == "assistant" and record.get("prompt_tokens"):
            if last is not None and record["prompt_tokens"] >= last:
                grown.append(record["prompt_tokens"] - last)
            last = record["prompt_tokens"]
    grown = grown[-PACE_STEPS:]
    return sum(grown) / len(grown) if grown else None


def message_bytes(message):
    """A message's bytes, as it is sent."""
    return len(json.dumps(message, ensure_ascii=False).encode("utf-8"))


def bytes_a_token(records):
    """The bytes a token of this session's text, as its model server counts
    it: what came between two counted calls in bytes, over the growth of the
    server's count, the least of it over the newest PACE_STEPS steps of
    LEAST_STEP_BYTES or more (the densest text a step held, so the estimate
    is high rather than low), never under one; each pair of calls on one
    context, since the latest model switch. None before a step has
    counted."""
    found, later, between = [], None, 0
    for record in reversed(records):
        kind = record["type"]
        if kind == "session" or (kind == "system" and record.get("why") == "model"):
            break
        if kind in history.SYSTEM_RECORDS:
            later, between = None, 0
            continue
        message = history.message(record)
        between += message_bytes(message) if message is not None else 0
        if kind != "assistant" or not record.get("prompt_tokens"):
            continue
        grown = (later or 0) - record["prompt_tokens"]
        if later is not None and grown > 0 and between >= LEAST_STEP_BYTES:
            found.append(between / grown)
            if len(found) == PACE_STEPS:
                break
        later, between = record["prompt_tokens"], 0
    return max(min(found), 1) if found else None


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
