"""How big the context is, and how much room it has.

A call's size is known after it: the server counts the prompt's tokens.
What came since (the reply, the results, the user's next message) is
counted by the server where it can (/tokenize). Else, where the
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

from core import entries, replay

BYTES_PER_TOKEN = 1
# The steps a session's pace, and its bytes a token, are measured over: its
# newest
PACE_STEPS = 20
# A step with less between its two calls tells too little of the bytes a
# token: a message's own wrapping counts for more there
LEAST_STEP_BYTES = 1_000


def changes_model(entry):
    return ((entries.has(entry, "system_prompt") and entry.get("why") == "model")
            or (entries.has(entry, "note")
                and entry.get("kind") in ("fallback", "main_again")))


def tokens(text, bytes_a_token=BYTES_PER_TOKEN):
    """`text`'s tokens with no count from the server: its bytes over
    `bytes_a_token`, rounded up; at one a byte, never fewer than the
    server's."""
    return int(-(-len(text.encode("utf-8")) // bytes_a_token))


def entry_text(entry):
    """A model entry's visible contents and call arguments for sizing."""
    parts = []
    for node, origin, _, _ in replay.model_nodes(entry, stop_at="tool_result"):
        if entries.has(node, "tool_call"):
            if origin == "model":
                parts.append({
                    "call": entries.call_id(node), "name": node.get("name"),
                    "arguments": node.get("arguments"),
                })
        elif entries.has(node, "tool_result"):
            if origin == "tool":
                parts.append({"answer": entries.linked_to(node, "answers"),
                              "text": replay.tool_output(node)})
        elif entries.has(node, "model_visible") and not entries.has(
            node, "assistant_reply"
        ):
            parts.append({"origin": origin, "text": entries.project(node)})
    return json.dumps(parts, ensure_ascii=False) if parts else ""


def entry_tokens(entry, count=tokens):
    """Measure visible text and estimate opaque continuation locally."""
    public = entry_text(entry)
    private = sum(tokens(json.dumps(entries.thaw(node.fields), ensure_ascii=False))
                  for node in entries.matching(entry, "private_continuation"))
    return (count(public) if public else 0) + private


@dataclass(frozen=True)
class Size:
    """The context's tokens, its system message and tools included, and how
    many of them are estimated rather than counted by the server."""

    tokens: int
    estimated: int


def _schema_tokens(schemas, tokens):
    return tokens(json.dumps(schemas() if callable(schemas) else schemas,
                             ensure_ascii=False))


def measure(records, conversation, schemas, tokens=tokens):
    """The context's size: the last call's count plus what came after it
    (`tokens`: text's tokens, the server's count where there is one), when
    that call saw the conversation as it stands; else all of it so (no call
    yet, or a model switch or a compaction since). New code since that call
    rewrote the system message and the tools around the same conversation
    on the same model: the count holds, and those two are added whole, so
    their old copies are counted twice, never low. `schemas` can be a
    callable, used only when the tools' size needs estimating."""
    last, rewritten = None, False
    for record in reversed(records):
        entry = record.entry
        if entries.has(entry, "system_prompt") and entry.get("why") == "code":
            rewritten = True
        elif replay.starts_context(entry) or changes_model(entry):
            break
        elif entries.has(entry, "assistant_reply") and entry.get("prompt_tokens"):
            last = entry
            break
    if last is not None:
        for index in range(len(conversation) - 1, -1, -1):
            if conversation[index].id == last.id:
                guessed = sum(entry_tokens(entry, tokens)
                              for entry in conversation[index:])
                if rewritten:
                    guessed += entry_tokens(conversation[0], tokens)
                    guessed += _schema_tokens(schemas, tokens)
                return Size(last.get("prompt_tokens") + guessed, guessed)
    guessed = sum(entry_tokens(entry, tokens) for entry in conversation)
    guessed += _schema_tokens(schemas, tokens)
    return Size(guessed, guessed)


def fixed_tokens(records, conversation, schemas, tokens=tokens):
    """The tokens every context holds, its system message and tools: their
    count (`tokens`, a token a byte where the server counts nothing), but
    never more than the smallest prompt a call on this model has sent, which
    held them and more. A token a byte counted a start of 6.4k tokens as
    25k, and a fresh context's room as below zero (7.3's eval)."""
    found = tokens(entries.project(conversation[0])) + tokens(
        json.dumps(schemas, ensure_ascii=False))
    for record in reversed(records):
        entry = record.entry
        if changes_model(entry):
            break
        if entries.has(entry, "assistant_reply") and entry.get("prompt_tokens"):
            found = min(found, entry.get("prompt_tokens"))
    return found


def tokens_a_step(records):
    """The tokens a step adds to the context at the session's own pace: the
    mean growth of its server's count from one call to the next, over its
    newest PACE_STEPS steps, each pair of calls on one context (no
    compaction, model switch or new system message between); None before
    two calls have counted."""
    grown, last = [], None
    for record in reversed(records):
        entry = record.entry
        if replay.starts_context(entry) or changes_model(entry):
            last = None
        elif entries.has(entry, "assistant_reply") and (prompt := entry.get(
            "prompt_tokens"
        )):
            if last is not None and last >= prompt:
                grown.append(last - prompt)
                if len(grown) == PACE_STEPS:
                    break
            last = prompt
    return sum(grown) / len(grown) if grown else None


def entry_bytes(entry):
    """A message's bytes, as it is sent."""
    return len(entry_text(entry).encode("utf-8"))


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
        entry = record.entry
        if entries.has(entry, "header") or changes_model(entry):
            break
        if replay.starts_context(entry):
            later, between = None, 0
            continue
        projected = replay.model_entry(record)
        between += entry_bytes(projected) if projected is not None else 0
        prompt = entry.get("prompt_tokens")
        if not entries.has(entry, "assistant_reply") or not prompt:
            continue
        grown = (later or 0) - prompt
        if later is not None and grown > 0 and between >= LEAST_STEP_BYTES:
            found.append(between / grown)
            if len(found) == PACE_STEPS:
                break
        later, between = prompt, 0
    return max(min(found), 1) if found else None


def line(limits, window):
    """The tokens a session's context is kept under: its compact_at_tokens,
    or the model's window when unset, and never past the window."""
    chosen = limits.get("compact_at_tokens") or window
    return min(chosen, window)


def reply_room(at, limits, max_output_tokens=None):
    """The next reply's room under the line `at`: reply_room_tokens, at most
    an eighth of the line, so a small window keeps room for the talk."""
    return min(limits["reply_room_tokens"], at // 8,
               max_output_tokens if max_output_tokens is not None else at)


def handoff_room(at, limits, max_output_tokens=None):
    """A compaction handoff's room under the line `at`, its ceiling too:
    handoff_room_tokens, at most an eighth of the line."""
    return min(limits["handoff_room_tokens"], at // 8,
               max_output_tokens if max_output_tokens is not None else at)


def headroom(at, instructions, limits, max_output_tokens=None):
    """What must fit beside the context under the line `at`: the next reply,
    a compaction's `instructions` and its handoff. No margin: what the
    server didn't count is counted high."""
    return (reply_room(at, limits, max_output_tokens)
            + handoff_room(at, limits, max_output_tokens) + instructions)


def input_room(at, instructions, limits, *, input_limit=None, output_limit=None):
    chosen = at - headroom(at, instructions, limits, output_limit)
    return min(chosen, input_limit - instructions) if input_limit else chosen


def room_left(at, size, instructions, limits, **model_limits):
    """The tokens left before the context must be compacted; below zero, it
    must be now."""
    return input_room(at, instructions, limits, **model_limits) - size.tokens


def share(room, limits):
    """What a result ZERO didn't size may take of the room left:
    result_share_percent of it."""
    return max(room, 0) * limits["result_share_percent"] // 100


def fresh_room(at, fixed, instructions, limits, **model_limits):
    """The most one result can be: what a fresh context has, after its
    system message and tools (`fixed` tokens) and a handoff, below the line
    and the headroom."""
    return (input_room(at, instructions, limits, **model_limits) - fixed
            - handoff_room(at, limits, model_limits.get("output_limit")))


def model_limits(model):
    """Caps to reserve space under, taken from the model used for this call."""
    server = getattr(model, "server", None)
    return {"input_limit": getattr(server, "max_input_tokens", None),
            "output_limit": getattr(server, "max_output_tokens", None)}


class ModelLimitExceeded(Exception):
    """An inference request could not fit the model's verified limits."""


def has_input_bound(model):
    server = getattr(model, "server", None)
    return (getattr(server, "request_context_window", None) is not None
            or getattr(server, "max_input_tokens", None) is not None)


def bound_output(model, input_tokens, output_tokens=None, *, operation):
    """Output room left for this request under the endpoint's verified limits."""
    server = getattr(model, "server", None)
    total = getattr(server, "request_context_window", None)
    maximum_input = getattr(server, "max_input_tokens", None)
    maximum_output = getattr(server, "max_output_tokens", None)
    minimum = getattr(getattr(model, "api", None), "MIN_OUTPUT_TOKENS", 1)
    caps = (output_tokens, maximum_output, total - input_tokens if total else None)
    cap = min((cap for cap in caps if cap is not None), default=None)
    blocking = None
    if maximum_input and input_tokens > maximum_input:
        blocking = f"{maximum_input:,}-token input limit"
    elif total and input_tokens + minimum > total:
        blocking = f"{total:,}-token total context window"
    elif cap is not None and cap < minimum:
        blocking = f"{cap:,}-token output limit"
    if blocking:
        raise ModelLimitExceeded(
            f"{operation} exceeds {model.name}'s {blocking}. "
            "Switch to a model with more room and compact the session there."
        )
    return cap
