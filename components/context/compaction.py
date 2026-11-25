"""Making room in a session's context: old outputs cleared first, with no
model call, then a handoff for the older steps when that isn't enough.

The log keeps everything; a compaction is one record appended to it, and
the context becomes a view from there (core/replay.py): a system
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
import re
import time
from collections.abc import Mapping
from datetime import datetime
from uuid import uuid4

from components.context import results, room
from components.context.entries import ASKERS
from core import attribution, entries, replay, timeline, tools

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
{saved}{attachments}{asked}
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
ATTACHMENTS = (
    "\n## Attachments\n- [id]: [name] ([media_type]) [path]\n{lines}\n"
)
# The words the user or ZERO gave on what the handoff keeps (a compaction
# entry's `instructions`; whose, as replay.ASKERS names them), as the
# handoff call reads them, each line quoted, after the fixed form (OpenClaw's
# and Pi's "Additional focus", after theirs), so they add to it, never
# replace it
ASKED = (
    "\n{whose} gave these words on what the handoff keeps:\n{words}\n"
    "What they name is kept in the handoff in full detail, in the part of "
    "the form it belongs to.\n"
)
CLEARED = "[Output cleared from view: {tokens:,} tokens, saved whole in {path}.]"
CLEARED_MEDIA = (
    "[Tool output media cleared from inline view; attachment reference kept.]"
)
_ATTACHMENTS_SECTION_RE = re.compile(
    r"(?ms)^## Attachments[ \t]*(?:\n.*?)?(?=^## |\Z)",
)
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


def handoff_instructions(reason, saved="", asked="", keeps_answer=False,
                         attachments=""):
    """What the handoff call reads after the conversation, for a compaction
    for `reason` ("line", "refused", "user" or "zero"): why it comes, what the
    handoff replaces and whether what stays `keeps_answer` (begins with an
    answer), its form, then `saved` (SAVED, filled), `attachments`
    (ATTACHMENTS, filled) and `asked` (asked())."""
    replaced = (REPLACED_BEFORE_STEP if reason in ASKERS
                else REPLACED_BEFORE_STEPS)
    return INSTRUCTIONS.format(occasion=OCCASIONS[reason], replaced=replaced,
                               answered=ANSWER_KEPT if keeps_answer else "",
                               saved=saved, attachments=attachments, asked=asked)


# The room kept for them: the longest
INSTRUCTION_TOKENS = max(
    room.tokens(handoff_instructions(
        reason, saved=SAVED, attachments=ATTACHMENTS, keeps_answer=True,
    ))
    for reason in OCCASIONS)


def _iter_visible_attachment_nodes(entry):
    """Yield descendant `attachments/file` nodes reachable only via visible parents."""
    for child in entry.entries or ():
        if entries.has(child, "private_continuation"):
            continue
        if not entries.has(child, "model_visible"):
            continue
        if entries.has(child, "attachment"):
            yield child
        yield from _iter_visible_attachment_nodes(child)


def collect_active_attachments(previous, records):
    """Collect unique active attachment summaries in chronological order."""
    ordered = {}
    if previous is not None:
        for item in previous.get("attachments") or ():
            if not isinstance(item, Mapping):
                continue
            aid = item.get("attachment_id")
            if isinstance(aid, str) and aid:
                row = {
                    "attachment_id": aid,
                    "name": item.get("name") or aid,
                    "media_type": item.get("media_type") or "application/octet-stream",
                }
                if item.get("path"):
                    row["path"] = str(item["path"])
                ordered[aid] = row
    for record in records:
        if replay.model_entry(record) is None:
            continue
        for node in _iter_visible_attachment_nodes(record.entry):
            aid = node.get("attachment_id")
            if not isinstance(aid, str) or not aid:
                continue
            row = {
                "attachment_id": aid,
                "name": node.get("name") or aid,
                "media_type": node.get("media_type") or "application/octet-stream",
            }
            if node.get("path"):
                row["path"] = str(node.get("path"))
            ordered[aid] = row
    return list(ordered.values())


def format_attachment_lines(attachments):
    """Format active attachment summaries as compact bullet lines."""
    lines = []
    for item in attachments:
        aid = item["attachment_id"]
        name = item.get("name") or aid
        media = item.get("media_type") or "application/octet-stream"
        path = item.get("path")
        if path:
            lines.append(f"- {aid}: {name} ({media}) at {path}")
        else:
            lines.append(f"- {aid}: {name} ({media})")
    return "\n".join(lines)


def with_attachments_section(summary, attachments):
    """Deterministically attach the authoritative `## Attachments` section."""
    if not attachments:
        return summary
    cleaned = _ATTACHMENTS_SECTION_RE.sub("", summary or "").rstrip()
    section = f"## Attachments\n{format_attachment_lines(attachments)}"
    return f"{cleaned}\n\n{section}" if cleaned else section


def is_answer(record):
    """Whether `record` is a reply with no calls: an answer."""
    return entries.has(record.entry, "assistant_reply") and not any(
        replay.calls(record))


def asked(instructions):
    """The askers' words for the handoff's instructions, each marked with
    whose they are (`instructions`: {"user" or "zero": their words}); "" for
    none. They take room the headroom keeps for a reply (room.py), which
    the handoff call doesn't write: its own reply is the handoff."""
    return "".join(
        ASKED.format(whose=ASKERS[whose], words=replay.quoted(words))
        for whose, words in (instructions or {}).items() if (words or "").strip()
    )


class CompactionFailed(Exception):
    """No handoff came back; the context is left as it was."""


class NothingToCompact(CompactionFailed):
    """Nothing older than the newest step: a compaction can't make room."""


def leaves_room_for_steps(records, at, after, limits, **model_limits):
    """Whether a context of `after` tokens leaves room below the line `at`,
    past the headroom, for CLEARED_ENOUGH_STEPS steps at the pace `records`
    show (room.tokens_a_step); with no pace counted yet, any room."""
    left = room.input_room(at, INSTRUCTION_TOKENS, limits, **model_limits) - after
    return left >= CLEARED_ENOUGH_STEPS * (room.tokens_a_step(records) or 0)


def cut_points(body):
    """Where the kept steps may start: at a user message, or at a reply;
    never between a call and its result, nor between a message and the
    answer that ends its turn (the handoff would take the message for one
    still open)."""
    points = []
    before_input = False
    pending = {}
    for i, record in enumerate(body):
        entry = record.entry
        is_note = (entries.has(entry, "note") and not entry.get("record_type")
                   or entries.has(entry, "switch") and entry.get("kind") == "switch")
        if is_note and room.changes_model(entry):
            # Routing notes belong to the step the incoming model will finish.
            continue
        is_input = timeline.is_turn_input(entry) or is_note
        if not pending and is_input:
            points.append(i)
        elif entries.has(entry, "assistant_reply"):
            calls = tuple(replay.calls(record))
            if not pending and (calls or not before_input):
                points.append(i)
            for call in calls:
                call_id = entries.call_id(call)
                pending[call.id or call_id] = call_id
        elif entries.has(entry, "tool_result"):
            target = replay.result_target(record)
            if pending.pop(target, None) is None:
                for entry_id, call_id in tuple(pending.items()):
                    if call_id == target:
                        del pending[entry_id]
        if replay.model_entry(record) is not None:
            before_input = is_input
    return points


def kept_from(body, budget, tokens=room.tokens, *, by_id=None):
    """The index in `body` where the kept steps start: as far back as fits in
    `budget` tokens (`tokens`: text's tokens), but never before the second
    cut point (something must be left to hand off) nor after the newest
    one."""
    points = cut_points(body)
    if len(points) < 2:
        return None
    available = by_id or {}
    sizes = [room.entry_tokens(available.get(entry.id, entry), tokens)
             if (entry := replay.model_entry(record)) else 0 for record in body]
    chosen = points[-1]
    for point in reversed(points[1:]):
        if sum(sizes[point:]) > budget:
            break
        chosen = point
    return chosen


def tool_name(record):
    return record.entry.get("tool") or "tool"


def save_outputs(body, upto, folder, cleared, tokens=room.tokens):
    """The outputs already cleared, and each one before `upto` in `body` big
    enough to clear (`tokens`: text's tokens) or carrying stale inline media,
    saved whole: {seq: line}. An output its tool never clears (a skill's
    instructions) stays."""
    lines = dict(cleared)
    for record in body[:upto]:
        if (not entries.has(record.entry, "tool_result")
                or tools.has_trait(tool_name(record), "never_cleared")):
            continue
        seq = str(record.seq)
        if seq in lines:
            continue
        result = record.entry.get("result") or ""
        cost = tokens(result)
        has_media = room.entry_has_inline_media(record.entry)
        if cost > CLEAR_FROM_TOKENS:
            path = results.save(folder, f"{seq}-{tool_name(record)}.txt", result)
            lines[seq] = CLEARED.format(tokens=cost, path=path)
        elif has_media:
            lines[seq] = (
                f"{result}\n{CLEARED_MEDIA}" if result else CLEARED_MEDIA
            )
    return lines


def has_saved_file(line):
    """Whether a cleared line names a saved output file on disk."""
    return " saved whole in " in line


def path_in(line):
    """The file a cleared output's line names (CLEARED: its path, then ".]",
    cut once, so a path ending in a dot or a bracket keeps it)."""
    return line.rsplit(" saved whole in ", 1)[-1].removesuffix(".]")


def described(record):
    """An output's call as the handoff's list names it: its summary's first
    line."""
    arguments = record.entry.get("arguments") or ""
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        args = {}
    text = tools.summary(tool_name(record), args, arguments)
    return text.split("\n", 1)[0]


def has_conversation(records):
    """Whether a branch has at least one turn input to compact."""
    return any(attribution.is_turn_input(r.entry) for r in records)


def write_compaction(session, *, reason, summary, kept_from, tokens_before=None,
                     line=None, cleared=None, tokens_after=None, handoff_at=None,
                     handoff_call=None, saved=None, cleared_only=False,
                     fresh=False, occasion=None, instructions=None,
                     attachments=None):
    """Append a native compaction handoff entry and refresh the session view."""
    fields = {
        "reason": reason, "tokens_before": tokens_before, "line": line,
        "kept_from": kept_from, "cleared": cleared,
        "tokens_after": tokens_after, "handoff_at": handoff_at,
        "handoff_call": handoff_call, "saved": saved,
        "cleared_only": cleared_only or None, "fresh": fresh or None,
        "occasion": occasion, "instructions": instructions or None,
        "attachments": attachments or None,
        **session.marked(),
    }
    fields = {k: v for k, v in fields.items() if v is not None}
    children = [entries.make("system_prompt", fields={"text": session.system_text()})]
    if summary:
        children.append(entries.make("text", origin="model", fields={"text": summary}))
    else:
        fields["summary"] = None
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    entry = entries.make(
        "compaction_handoff", id=uuid4().hex, origin="system",
        created_at=stamp, fields=fields, entries=tuple(children),
    )
    entry = attribution.logged_interaction(entry, session.log.path.stem)
    session.log.path.parent.mkdir(parents=True, exist_ok=True)
    record = session.log.append(entry)
    session.restore_entries(session.log.branch())
    return record


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
    conversation = session.entries
    model_caps = getattr(session.model, "capabilities", None)
    before = room.measure(
        records, conversation, session.tool_schemas(), tokens,
        capabilities=model_caps,
    )
    body = replay.in_context(records)
    previous_record = replay.latest_context_boundary(records)
    previous = previous_record.entry if previous_record is not None else None
    cleared = dict(previous.get("cleared") or {}) if previous else {}
    summary = (replay.boundary_summary(previous) or None) if previous else None
    active_attachments = collect_active_attachments(previous, body)
    asked_for = reason in ASKERS
    by_id = {entry.id: entry for entry in conversation}
    start = kept_from(body, 0 if asked_for else int(at * TAIL_SHARE), tokens,
                      by_id=by_id)
    if start is None:
        raise NothingToCompact("nothing older than the newest step to hand off")
    schemas = session.tool_schemas()
    if room.has_input_bound(session.model):
        newest = body[cut_points(body)[-1]:]
        newest_entries = (conversation[0], *(
            by_id[entry.id] for record in newest
            if (entry := replay.model_entry(record)) is not None
        ))
        # Inline media is withholdable on the next request; only irreducible
        # text and compact references exceeding the bound should fail.
        size = room.measure(
            (), newest_entries, schemas, tokens, include_media=False,
        )
        room.bound_output(session.model, size.tokens, operation="The newest step")
    caps = room.model_limits(session.model)
    instructions = {whose: words.strip() for whose, words in
                    (instructions or {}).items() if (words or "").strip()}
    fields = {"reason": reason, "tokens_before": before.tokens, "line": at}
    if instructions:
        fields["instructions"] = instructions
    # Clearing first: the older outputs saved whole, lines in their place
    lines = save_outputs(body, start, folder, cleared, tokens)
    kept = previous.get("kept_from") if previous else body[0].seq
    view = [(record, by_id[entry.id]) for record in body
            if (entry := replay.model_entry(record)) is not None]
    after = tokens(entries.project(conversation[0]))
    after += tokens(json.dumps(schemas, ensure_ascii=False))
    if summary:
        if active_attachments:
            summary = with_attachments_section(summary, active_attachments)
        after += tokens(summary)
    for record, entry in view:
        seq = str(record.seq)
        if seq in lines:
            entry = replay.with_result(entry, lines[seq])
        after += room.entry_tokens(entry, tokens, capabilities=model_caps)
    # At the line, enough when it cleared something and leaves room for the
    # next steps; a clearing of nothing that looks enough means the estimate
    # is off from the server's count, which the handoff trusts
    cleared_some = len(lines) > len(cleared)
    if (reason == "line" and cleared_some
            and leaves_room_for_steps(records, at, after, limits, **caps)):
        if summary:
            # The handoff stays, with the time it was written
            fields["handoff_at"] = previous.get("handoff_at") or previous.created_at
        return write_compaction(
            session, **fields, summary=summary, kept_from=kept, cleared=lines,
            tokens_after=after, cleared_only=True,
            attachments=active_attachments or None,
        )
    # Then a handoff: the conversation before the kept steps, as last sent
    offset = len(conversation) - len(view)
    earlier = [(record, entry) for record, entry in view
               if record.seq < body[start].seq]
    saved = "\n".join(
        f"- {described(r)}: {path_in(lines[str(r.seq)])}"
        for r in body[:start]
        if str(r.seq) in lines and has_saved_file(lines[str(r.seq)])
    )
    att_block = (
        ATTACHMENTS.format(lines=format_attachment_lines(active_attachments))
        if active_attachments else ""
    )
    ask = handoff_instructions(
        reason, saved=SAVED.format(lines=saved) if saved else "",
        attachments=att_block,
        asked=asked(instructions), keeps_answer=is_answer(body[start]))
    request = handoff_request(session, ask)
    prefix = conversation[:offset]
    handoff_entries = prefix + tuple(entry for _, entry in earlier) + (request,)
    cap = room.handoff_room(at, limits, caps["output_limit"])
    h_schemas = _handoff_schemas(session)
    prepared = _prepare_handoff(session, handoff_entries, h_schemas, tokens, cap)
    try:
        cap = handoff_output(
            session, handoff_entries, h_schemas, tokens, cap, prepared=prepared,
        )
    except room.ModelLimitExceeded:
        if not saved and not any(str(r.seq) in lines for r, _ in earlier):
            raise
        # The original tool results remain in the log and the saved files.
        reduced = tuple(replay.with_result(entry, lines[str(record.seq)])
                        if str(record.seq) in lines else entry
                        for record, entry in earlier)
        handoff_entries = (*prefix, *reduced, request)
        prepared = _prepare_handoff(
            session, handoff_entries, h_schemas, tokens, cap,
        )
        cap = handoff_output(
            session, handoff_entries, h_schemas, tokens, cap, prepared=prepared,
        )
    t0 = time.monotonic()
    reply = handoff_call(session, handoff_entries, cap, check, prepared=prepared)
    summary = (reply.content or "").strip()
    if not summary:
        raise CompactionFailed("the handoff call came back empty")
    summary = with_attachments_section(summary, active_attachments)
    fields["handoff_call"] = {
        "prompt_tokens": reply.prompt_tokens, "cached_tokens": reply.cached_tokens,
        "completion_tokens": reply.completion_tokens,
        "seconds": round(time.monotonic() - t0, 1),
    }
    # The kept steps' outputs cleared before stay cleared
    first = body[start].seq
    still = {seq: text for seq, text in lines.items() if int(seq) >= first}
    tail = [replay.with_result(entry, still[str(record.seq)])
            if str(record.seq) in still else entry
            for record, entry in view if record.seq >= first]
    after = tokens(entries.project(conversation[0])) + tokens(summary)
    after += tokens(json.dumps(schemas, ensure_ascii=False))
    after += sum(room.entry_tokens(m, tokens, capabilities=model_caps) for m in tail)
    saved_files = sorted(line for line in lines.values() if has_saved_file(line))
    return write_compaction(
        session, **fields, summary=summary, kept_from=first, cleared=still,
        saved=saved_files, tokens_after=after,
        attachments=active_attachments or None,
    )


# What a round's handoff call reads after the conversation, when the round
# ended without one of its own (rounds/): nothing of it stays
ROUND = INSTRUCTIONS.format(
    occasion="this round of the work in rounds has ended",
    replaced="the whole conversation is about to be replaced by a handoff that "
             "you write now; the next round starts from it.",
    answered="", saved="", attachments="", asked="",
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
    conversation = session.entries
    body = replay.in_context(records)
    previous_record = replay.latest_context_boundary(records)
    previous = previous_record.entry if previous_record is not None else None
    active_attachments = collect_active_attachments(previous, body)
    model_caps = getattr(session.model, "capabilities", None)
    before = room.measure(
        records, conversation, session.tool_schemas(), tokens,
        capabilities=model_caps,
    )
    fields = {"reason": reason, "tokens_before": before.tokens, "line": at,
              "fresh": True, "occasion": occasion}
    if not (handoff or "").strip():
        att_block = (
            ATTACHMENTS.format(lines=format_attachment_lines(active_attachments))
            if active_attachments else ""
        )
        round_ask = INSTRUCTIONS.format(
            occasion="this round of the work in rounds has ended",
            replaced="the whole conversation is about to be replaced by a "
                     "handoff that you write now; the next round starts from it.",
            answered="", saved="", attachments=att_block, asked="",
        )
        handoff_entries = (*conversation, handoff_request(session, round_ask))
        caps = room.model_limits(session.model)
        cap = room.handoff_room(at, limits, caps["output_limit"])
        h_schemas = _handoff_schemas(session)
        prepared = _prepare_handoff(
            session, handoff_entries, h_schemas, tokens, cap,
        )
        cap = handoff_output(
            session, handoff_entries, h_schemas, tokens, cap, prepared=prepared,
        )
        t0 = time.monotonic()
        reply = handoff_call(
            session, handoff_entries, cap, check, prepared=prepared,
        )
        handoff = (reply.content or "").strip()
        if not handoff:
            raise CompactionFailed("the round's handoff call came back empty")
        fields["handoff_call"] = {
            "prompt_tokens": reply.prompt_tokens,
            "cached_tokens": reply.cached_tokens,
            "completion_tokens": reply.completion_tokens,
            "seconds": round(time.monotonic() - t0, 1),
        }
    handoff = with_attachments_section(handoff.strip(), active_attachments)
    after = tokens(entries.project(conversation[0])) + tokens(handoff)
    after += tokens(json.dumps(session.tool_schemas(), ensure_ascii=False))
    # Nothing before the compaction stays: the context starts at its own
    # record, the next one appended
    return write_compaction(
        session, **fields, summary=handoff, kept_from=records[-1].seq + 1,
        cleared={}, tokens_after=after,
        attachments=active_attachments or None,
    )


def handoff_request(session, text):
    return entries.root("note", "system", text=text)


def _handoff_schemas(session):
    schemas = session.tool_schemas()
    boundary = session.tool_context.get("tool_boundary")
    if boundary is not None:
        schemas = boundary.redact(schemas)
    return schemas


def _prepare_handoff(session, messages, schemas, tokens, cap):
    """Bound inline media inside a compaction handoff call to its input room."""
    prepare = getattr(session.model, "prepare_request", None)
    if prepare is None or not room.has_attachments(messages):
        return None
    server = getattr(session.model, "server", None)
    window = (
        getattr(server, "request_context_window", None)
        or getattr(server, "context_window", None)
    )
    max_in = getattr(server, "max_input_tokens", None)
    budgets = []
    if window is not None:
        budgets.append(max(window - (cap or 0), 0))
    if max_in is not None:
        budgets.append(max_in)
    total_budget = min(budgets) if budgets else None
    text_size = room.measure((), messages, schemas, tokens, include_media=False)
    media_budget = (
        max(total_budget - text_size.tokens, 0)
        if total_budget is not None else None
    )
    return prepare(
        messages, schemas, max_output_tokens=cap, tool_choice="none",
        media_budget=media_budget, total_budget=total_budget,
    )


def handoff_output(session, messages, schemas, tokens, cap, *, prepared=None):
    """The cap on the handoff's reply after checking its complete input."""
    if not room.has_input_bound(session.model):
        return cap
    size = room.measure((), messages, schemas, tokens, prepared=prepared)
    return room.bound_output(session.model, size.tokens, cap,
                             operation="The compaction handoff")


def handoff_call(session, messages, cap, check=None, *, prepared=None):
    """The handoff, written by the session's own model, `cap` tokens at most.
    The redacted tools stay attached with `tool_choice="none"` so the prompt
    prefix remains cached; any tool calls returned anyway are rejected without
    executing or retrying without tools."""
    schemas = _handoff_schemas(session)
    extra = {"prepared": prepared} if prepared is not None else {}
    reply = session.model.call(
        messages, schemas, check=check, max_output_tokens=cap,
        tool_choice="none", **extra,
    )
    if reply.tool_calls:
        raise CompactionFailed(
            "the handoff call returned tool calls despite tool_choice='none'"
        )
    return reply
