"""Making room in a session's context: old outputs cleared first, with no
model call, then a handoff for the older steps when that isn't enough.

The log keeps everything; a compaction is one record appended to it, and
the context becomes a view from there (core/history.py, replay): a system
message written again as things stand, the handoff, the steps kept as they
are, and what comes after. At the line the newest steps stay whole (a share
of the line, never fewer than the newest step); a compaction the user or
ZERO asks for keeps the newest step alone (a user's is often meant to drop a
wrong turn). A tool call never loses its result.

Clearing: each older output is saved whole in the session's results folder
and left in the context as a line naming the file. At the line, if that
leaves room for the next few steps at the session's own pace, it is the
compaction: clearing can be undone and a handoff can't. Otherwise, and
always when the user or ZERO asks (best effort) or a server refused the
request (its count was higher than the estimate), the steps before the kept
ones get a handoff, written by the model from the conversation as it
stands: the call is a prefix of the last request with the instructions
after it, so the server's cache serves it (DSH's way), and its older
outputs are saved first so the handoff can name their files. The user's or
ZERO's words on what it keeps follow the fixed form, marked with whose they
are, and never take its place.
"""

import json
import time

from context import results, room
from core import history, tools

# What the handoff call reads after the conversation. It states what the
# handoff is for and what goes in it, as the other harnesses' do (Codex,
# opencode, hermes, openclaw, DSH: goal, state, decisions, next step), as a
# template to fill (opencode's), so the headings come back bare; first why
# it comes and what it replaces (handoff_instructions)
INSTRUCTIONS = """[Compaction: {occasion}, so {replaced}{answered} You will \
read the handoff in place of the conversation so far; the session's log keeps all of \
it.

The handoff takes this form, each [bracket] replaced by what it describes, or \
by "(none)":

## The user's requests
- "[a request or correction, in the user's own words]": [what became of it] \
(every one, oldest first)

## Constraints and preferences
- [limits, preferences and scope the user set; what they said about themselves \
or the work]

## Done
- [what was done and found, with the results that matter]

## State
- [files and folders involved, with their paths; running jobs; decisions made \
and why]

## Next
- [what was under way when this was written; the next step]
{saved}{asked}
Paths, commands, error text, numbers and names in the handoff are copied \
exactly. Write only the handoff.]"""
# Why a compaction comes, by its reason, as the handoff call reads it
OCCASIONS = {
    "line": "this conversation's context is near its line",
    "refused": "the model server refused this conversation's context as too "
               "long",
    "user": "the user asked for a compaction",
    "zero": "you asked for a compaction",
}
# What the handoff replaces: at the line, everything before the newest steps;
# asked for, everything before the newest step
REPLACED_BEFORE_STEPS = (
    "everything before the newest steps is about to be replaced by a handoff "
    "that you write now. The newest steps stay as they are after it."
)
REPLACED_BEFORE_STEP = (
    "everything before the newest step is about to be replaced by a handoff "
    "that you write now. The newest step stays as it is after it."
)
# Where what stays begins with an answer: the handoff's writer doesn't see
# it, and took the request before it for one still open (7.3.1's eval)
ANSWER_KEPT = (
    " What stays begins with your answer to the last request before it: that "
    "request is answered."
)
SAVED = (
    "\n## Saved outputs\n- [a saved output that matters]: [its path]\n\n"
    "These outputs are saved whole:\n{lines}\n"
)
# The words the user or ZERO gave on what the handoff keeps (a compaction
# record's `instructions`; whose, as history.ASKERS names them), as the
# handoff call reads them, each line quoted, after the fixed form (OpenClaw's
# and Pi's "Additional focus", after theirs), so they add to it, never
# replace it
ASKED = (
    "\n{whose} gave these words on what the handoff keeps:\n{words}\n"
    "What they name is kept in the handoff in full detail, in the part of "
    "the form it belongs to.\n"
)
CLEARED = "[Output cleared from view: {tokens:,} tokens, saved whole in {path}.]"
# The newest steps kept as they are at the line: this share of it
TAIL_SHARE = 0.2
# An older output this many tokens or fewer stays as it is
CLEAR_FROM_TOKENS = 100
# At the line, clearing alone is the compaction when it leaves room for this
# many steps at the session's own pace (room.tokens_a_step), in its own
# counted tokens: clearing can be undone and a handoff can't (the line is
# the budget, where it sits the place for a model's cost and speed).
# Measured at 7.3.1 (evals/compaction_replay.py: 83 real sessions at lines
# of 32k to 262k): three steps hand off 30% to 56% less than 7.3's half the
# line, for 11% to 32% more read anew; two hand off little less again, for
# more compactions
CLEARED_ENOUGH_STEPS = 3


def handoff_instructions(reason, saved="", asked="", keeps_answer=False):
    """What the handoff call reads after the conversation, for a compaction
    for `reason` ("line", "refused", "user" or "zero"): why it comes, what the
    handoff replaces and whether what stays `keeps_answer` (begins with an
    answer), its form, then `saved` (SAVED, filled) and `asked` (asked())."""
    replaced = (REPLACED_BEFORE_STEP if reason in history.ASKERS
                else REPLACED_BEFORE_STEPS)
    return INSTRUCTIONS.format(occasion=OCCASIONS[reason], replaced=replaced,
                               answered=ANSWER_KEPT if keeps_answer else "",
                               saved=saved, asked=asked)


# The room kept for them: the longest
INSTRUCTION_TOKENS = max(
    room.tokens(handoff_instructions(reason, saved=SAVED, keeps_answer=True))
    for reason in OCCASIONS)


def is_answer(record):
    """Whether `record` is a reply with no calls: an answer."""
    return record["type"] == "assistant" and not (
        (record.get("message") or {}).get("tool_calls"))


def asked(instructions):
    """The askers' words for the handoff's instructions, each marked with
    whose they are (`instructions`: {"user" or "zero": their words}); "" for
    none. They take room the headroom keeps for a reply (room.py), which
    the handoff call doesn't write: its own reply is the handoff."""
    return "".join(
        ASKED.format(whose=history.ASKERS[whose], words=history.quoted(words))
        for whose, words in (instructions or {}).items() if (words or "").strip()
    )


class CompactionFailed(Exception):
    """No handoff came back; the context is left as it was."""


class NothingToCompact(CompactionFailed):
    """Nothing older than the newest step: a compaction can't make room."""


def leaves_room_for_steps(records, at, after, limits):
    """Whether a context of `after` tokens leaves room below the line `at`,
    past the headroom, for CLEARED_ENOUGH_STEPS steps at the pace `records`
    show (room.tokens_a_step); with no pace counted yet, any room."""
    left = at - room.headroom(at, INSTRUCTION_TOKENS, limits) - after
    return left >= CLEARED_ENOUGH_STEPS * (room.tokens_a_step(records) or 0)


def cut_points(body):
    """Where the kept steps may start: at a user message, or at a reply;
    never between a call and its result, nor between a message and the
    answer that ends its turn (the handoff would take the message for one
    still open)."""
    points = []
    before = None
    for i, record in enumerate(body):
        kind = record["type"]
        if kind in ("user", "note"):
            points.append(i)
        elif kind == "assistant":
            calls = (record.get("message") or {}).get("tool_calls")
            if calls or before not in ("user", "note"):
                points.append(i)
        if history.message(record) is not None:
            before = kind
    return points


def kept_from(body, budget, tokens=room.tokens):
    """The index in `body` where the kept steps start: as far back as fits in
    `budget` tokens (`tokens`: text's tokens), but never before the second
    cut point (something must be left to hand off) nor after the newest
    one."""
    points = cut_points(body)
    if len(points) < 2:
        return None
    sizes = [room.message_tokens(m, tokens) if (m := history.message(r)) else 0
             for r in body]
    chosen = points[-1]
    for point in reversed(points[1:]):
        if sum(sizes[point:]) > budget:
            break
        chosen = point
    return chosen


def tool_name(record):
    return record.get("tool") or "tool"


def save_outputs(body, upto, folder, cleared, tokens=room.tokens):
    """The outputs already cleared, and each one before `upto` in `body` big
    enough to clear (`tokens`: text's tokens), saved whole: {seq: line}."""
    lines = dict(cleared)
    for record in body[:upto]:
        if record["type"] != "tool":
            continue
        seq = str(record["seq"])
        if seq in lines:
            continue
        result = record.get("result") or ""
        cost = tokens(result)
        if cost <= CLEAR_FROM_TOKENS:
            continue
        path = results.save(folder, f"{seq}-{tool_name(record)}.txt", result)
        lines[seq] = CLEARED.format(tokens=cost, path=path)
    return lines


def path_in(line):
    """The file a cleared output's line names (CLEARED: its path, then ".]",
    cut once, so a path ending in a dot or a bracket keeps it)."""
    return line.rsplit(" saved whole in ", 1)[-1].removesuffix(".]")


def described(record):
    """An output's call as the handoff's list names it: its summary's first
    line."""
    try:
        args = json.loads(record.get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {}
    text = tools.summary(tool_name(record), args, record.get("arguments") or "")
    return text.split("\n", 1)[0]


def compact(session, reason, at, folder, check=None, tokens=room.tokens, *,
            limits, instructions=None):
    """Make room in `session`'s context under the line `at`; the compaction
    record. `reason`: "line", "zero", "user" or "refused"; `tokens`: text's
    tokens (the server's count where there is one); `limits`: the session's,
    for the handoff's room; `instructions`: the askers' words on what the
    handoff keeps ({"user" or "zero": their words}). One the user or ZERO
    asks for always gets a handoff, keeping the newest step alone. Raises
    CompactionFailed when no handoff comes back, NothingToCompact when there
    is nothing to hand off."""
    records = session.log.branch()
    before = room.measure(records, session.messages, session.tool_schemas(),
                          tokens)
    body = history.in_context(records)
    previous = history.latest_compaction(records)
    cleared = dict(previous.get("cleared") or {}) if previous else {}
    asked_for = reason in history.ASKERS
    start = kept_from(body, 0 if asked_for else int(at * TAIL_SHARE), tokens)
    if start is None:
        raise NothingToCompact("nothing older than the newest step to hand off")
    instructions = {whose: words.strip() for whose, words in
                    (instructions or {}).items() if (words or "").strip()}
    fields = {"reason": reason, "tokens_before": before.tokens, "line": at}
    if instructions:
        fields["instructions"] = instructions
    # Clearing first: the older outputs saved whole, lines in their place
    lines = save_outputs(body, start, folder, cleared, tokens)
    kept = previous["kept_from"] if previous else body[0]["seq"]
    view = [m for r in body if (m := history.message(r)) is not None]
    after = tokens(session.messages[0]["content"])
    after += tokens(json.dumps(session.tool_schemas(), ensure_ascii=False))
    if previous and previous.get("summary"):
        after += tokens(previous["summary"])
    for record, found in zip([r for r in body if history.message(r)], view):
        seq = str(record["seq"])
        if seq in lines:
            found = {**found, "content": lines[seq]}
        after += room.message_tokens(found, tokens)
    # At the line, enough when it cleared something and leaves room for the
    # next steps; a clearing of nothing that looks enough means the estimate
    # is off from the server's count, which the handoff trusts
    cleared_some = len(lines) > len(cleared)
    if (reason == "line" and cleared_some
            and leaves_room_for_steps(records, at, after, limits)):
        summary = previous.get("summary") if previous else None
        if summary:
            # The handoff stays, with the time it was written
            fields["handoff_at"] = previous.get("handoff_at") or previous["time"]
        return session.compacted(
            **fields, summary=summary, kept_from=kept, cleared=lines,
            tokens_after=after, cleared_only=True,
        )
    # Then a handoff: the conversation before the kept steps, as last sent
    offset = len(session.messages) - len(view)
    upto = offset + len([r for r in body[:start] if history.message(r)])
    saved = "\n".join(
        f"- {described(r)}: {path_in(lines[str(r['seq'])])}"
        for r in body[:start] if str(r.get("seq")) in lines
    )
    ask = handoff_instructions(
        reason, saved=SAVED.format(lines=saved) if saved else "",
        asked=asked(instructions), keeps_answer=is_answer(body[start]))
    messages = session.messages[:upto] + [{"role": "user", "content": ask}]
    t0 = time.monotonic()
    reply = handoff_call(session, messages, room.handoff_room(at, limits), check)
    summary = (reply.content or "").strip()
    if not summary:
        raise CompactionFailed("the handoff call came back empty")
    fields["handoff_call"] = {
        "prompt_tokens": reply.prompt_tokens, "cached_tokens": reply.cached_tokens,
        "completion_tokens": reply.completion_tokens,
        "seconds": round(time.monotonic() - t0, 1),
    }
    # The kept steps' outputs cleared before stay cleared
    first = body[start]["seq"]
    still = {seq: text for seq, text in lines.items() if int(seq) >= first}
    tail = []
    for record in body[start:]:
        found = history.message(record)
        if found is not None and str(record["seq"]) in still:
            found = {**found, "content": still[str(record["seq"])]}
        if found is not None:
            tail.append(found)
    after = tokens(session.messages[0]["content"]) + tokens(summary)
    after += tokens(json.dumps(session.tool_schemas(), ensure_ascii=False))
    after += sum(room.message_tokens(m, tokens) for m in tail)
    return session.compacted(
        **fields, summary=summary, kept_from=first, cleared=still,
        saved=sorted(lines.values()), tokens_after=after,
    )


# What a round's handoff call reads after the conversation, when the round
# ended without one of its own (rounds/): nothing of it stays
ROUND = INSTRUCTIONS.format(
    occasion="this round of the work in rounds has ended",
    replaced="the whole conversation is about to be replaced by a handoff that "
             "you write now; the next round starts from it.",
    answered="", saved="", asked="",
)


def fresh(session, handoff, at, tokens=room.tokens, check=None, *, limits,
          reason, occasion=None):
    """A compaction that keeps nothing but the system message and `handoff`
    (a round's end: the round's own, from end_round); with none, the model
    writes one from the whole conversation, as a compaction's, within the
    handoff's room (`limits`, the session's). `reason`: who asked;
    `occasion`: what happened, in the asker's words. The log keeps
    everything. Raises CompactionFailed when no handoff comes back."""
    records = session.log.branch()
    before = room.measure(records, session.messages, session.tool_schemas(),
                          tokens)
    fields = {"reason": reason, "tokens_before": before.tokens, "line": at,
              "fresh": True, "occasion": occasion}
    if not (handoff or "").strip():
        ask = ROUND
        t0 = time.monotonic()
        reply = handoff_call(session, session.messages + [
            {"role": "user", "content": ask}], room.handoff_room(at, limits),
            check)
        handoff = (reply.content or "").strip()
        if not handoff:
            raise CompactionFailed("the round's handoff call came back empty")
        fields["handoff_call"] = {
            "prompt_tokens": reply.prompt_tokens,
            "cached_tokens": reply.cached_tokens,
            "completion_tokens": reply.completion_tokens,
            "seconds": round(time.monotonic() - t0, 1),
        }
    after = tokens(session.messages[0]["content"]) + tokens(handoff)
    after += tokens(json.dumps(session.tool_schemas(), ensure_ascii=False))
    # Nothing before the compaction stays: the context starts at its own
    # record, the next one appended
    return session.compacted(
        **fields, summary=handoff.strip(), kept_from=records[-1]["seq"] + 1,
        cleared={}, tokens_after=after,
    )


def handoff_call(session, messages, cap, check=None):
    """The handoff, written by the session's own model, `cap` tokens at most.
    The tools are sent as the last request sent them, so the cache serves the
    prefix; a reply that calls a tool instead is asked again without them."""
    reply = session.model.call(messages, session.tool_schemas(), check=check,
                               max_output_tokens=cap)
    if not (reply.content or "").strip() and reply.tool_calls:
        reply = session.model.call(messages, [], check=check,
                                   max_output_tokens=cap)
    return reply
