"""Making room in a session's context: old outputs cleared first, with no
model call, then a handoff for the older steps when that isn't enough.

The log keeps everything; a compaction is one record appended to it, and
the context becomes a view from there (core/history.py, replay): a system
message written again as things stand, the handoff, the steps kept as they
are, and what comes after. The newest steps stay whole (a share of the line,
never fewer than the newest step), and a tool call never loses its result.

Clearing: each older output is saved whole in the session's results folder
and left in the context as a line naming the file. If the context is then
under half the line, that is the compaction (never after a server refused
the request: its count was higher than the estimate); otherwise the steps before
the kept ones get a handoff, written by the model from the conversation as
it stands: the call is a prefix of the last request with the instructions
after it, so the server's cache serves it (DSH's way), and its older
outputs are saved first so the handoff can name their files.
"""

import json
import time

from context import results, room
from core import history, tools

# What the handoff call reads after the conversation. It states what the
# handoff is for and what goes in it, as the other harnesses' do (Codex,
# opencode, hermes, openclaw, DSH: goal, state, decisions, next step), as a
# template to fill (opencode's), so the headings come back bare
INSTRUCTIONS = """[Compaction: this conversation's context is near its line, so \
everything before the newest steps is about to be replaced by a handoff that \
you write now. The newest steps stay as they are after it. You will read the \
handoff in place of the conversation so far; the session's log keeps all of \
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
{saved}
Paths, commands, error text, numbers and names in the handoff are copied \
exactly. Write only the handoff.]"""
SAVED = (
    "\n## Saved outputs\n- [a saved output that matters]: [its path]\n\n"
    "These outputs are saved whole:\n{lines}\n"
)
CLEARED = "[Output cleared from view: {tokens:,} tokens, saved whole in {path}.]"
# The newest steps kept as they are: this share of the line
TAIL_SHARE = 0.2
# An older output this many tokens or fewer stays as it is
CLEAR_FROM_TOKENS = 100
# Clearing alone is the compaction when it leaves the context under this
# share of the line
CLEARED_ENOUGH = 0.5
INSTRUCTION_TOKENS = room.tokens(INSTRUCTIONS.format(saved=SAVED))


class CompactionFailed(Exception):
    """No handoff came back; the context is left as it was."""


class NothingToCompact(CompactionFailed):
    """Nothing older than the newest step: a compaction can't make room."""


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


def kept_from(body, budget):
    """The index in `body` where the kept steps start: as far back as fits in
    `budget` tokens, but never before the second cut point (something must
    be left to hand off) nor after the newest one."""
    points = cut_points(body)
    if len(points) < 2:
        return None
    sizes = [room.message_tokens(m) if (m := history.message(r)) else 0
             for r in body]
    chosen = points[-1]
    for point in reversed(points[1:]):
        if sum(sizes[point:]) > budget:
            break
        chosen = point
    return chosen


def tool_name(record):
    return record.get("tool") or "tool"


def save_outputs(body, upto, folder, cleared):
    """The outputs already cleared, and each one before `upto` in `body` big
    enough to clear, saved whole: {seq: line}."""
    lines = dict(cleared)
    for record in body[:upto]:
        if record["type"] != "tool":
            continue
        seq = str(record["seq"])
        if seq in lines:
            continue
        result = record.get("result") or ""
        cost = room.tokens(result)
        if cost <= CLEAR_FROM_TOKENS:
            continue
        path = results.save(folder, f"{seq}-{tool_name(record)}.txt", result)
        lines[seq] = CLEARED.format(tokens=cost, path=path)
    return lines


def path_in(line):
    """The file a cleared output's line names."""
    return line.rsplit(" saved whole in ", 1)[-1].rstrip(".]")


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
            limits):
    """Make room in `session`'s context under the line `at`; the compaction
    record. `reason`: "line", "zero", "user" or "refused"; `tokens`: text's
    tokens (the server's count where there is one); `limits`: the session's,
    for the handoff's room. Raises
    CompactionFailed when no handoff comes back, NothingToCompact when there
    is nothing to hand off."""
    records = session.log.branch()
    before = room.measure(records, session.messages, session.tool_schemas(),
                          tokens)
    body = history.in_context(records)
    previous = history.latest_compaction(records)
    cleared = dict(previous.get("cleared") or {}) if previous else {}
    start = kept_from(body, int(at * TAIL_SHARE))
    if start is None:
        raise NothingToCompact("nothing older than the newest step to hand off")
    fields = {"reason": reason, "tokens_before": before.tokens, "line": at}
    # Clearing first: the older outputs saved whole, lines in their place
    lines = save_outputs(body, start, folder, cleared)
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
    # Enough when it cleared something and leaves half the line; a clearing
    # of nothing that looks enough means the estimate is off from the
    # server's count, which the handoff trusts
    cleared_some = len(lines) > len(cleared)
    if cleared_some and after <= at * CLEARED_ENOUGH and reason != "refused":
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
    ask = INSTRUCTIONS.format(saved=SAVED.format(lines=saved) if saved else "")
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
ROUND = INSTRUCTIONS.replace(
    "this conversation's context is near its line, so everything before the "
    "newest steps is about to be replaced by a handoff that you write now. The "
    "newest steps stay as they are after it.",
    "this round of the work in rounds has ended, so the whole conversation is "
    "about to be replaced by a handoff that you write now; the next round "
    "starts from it.",
)


def fresh(session, handoff, at, tokens=room.tokens, check=None, *, limits):
    """A round's end: the context keeps nothing but the system message and
    `handoff` (the round's own, from end_round); with none, the model writes
    one from the whole conversation, as a compaction's, within the handoff's
    room (`limits`, the session's). The log keeps everything. Raises
    CompactionFailed when no handoff comes back."""
    records = session.log.branch()
    before = room.measure(records, session.messages, session.tool_schemas(),
                          tokens)
    fields = {"reason": "round", "tokens_before": before.tokens, "line": at}
    if not (handoff or "").strip():
        ask = ROUND.format(saved="")
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
