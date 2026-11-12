"""The judge as the auto modes' approver: what it sees of a session (the
user's words, ZERO's notices, the commands that ran with their latest output)
and what its verdict does to a call. The judge itself is verdict.py.

The proposed call comes whole (a write's content, an edit's strings), the
commands that ran as an ask shows them, each with its output cut in the
middle. The view only grows, so each call starts with the one before and
the server's cache serves it (95% of each call's history in long
sessions, against 42% when only the latest outputs showed). A long session's
view keeps every user message and notice; past the judge's room, its oldest
commands are left out, and one line says how many.

In a session tree (subagents/), the goal is the user's words anywhere in
it: the server hands the session `tree_user_messages()`, the user's
messages in the tree's other sessions, and where this session's own records
start (a subagent's fork copies its parent's, which the tree's messages
already hold). A task ZERO wrote reaches the judge as a notice,
never as the user's words.
"""

import json
from types import SimpleNamespace

from core import history as log_history
from core import tools
from core.asking import ask_of
from core.hooks import Deny, Hook, Stop

# Claude Code's auto mode, Codex and Hermes: this many refusals in a row end
# the turn. None counted per turn: refusals spread over hours aren't a loop
MAX_REFUSAL_STREAK = 3
DENIED = "Not run: the judge said no: {reason}."
NO_VERDICT = "Not run: the judge gave no verdict ({error})."
SIDE_EFFECTS = (
    "[The judge allowed this with side effects the goal can't avoid: {effects}.]"
)


# What a call crosses, in its tool's one sentence (its `crossing`), with the
# reason
CROSSING = "\n({}; reason: {})"
PAST_LIMIT = "\n({})"
LEFT_OUT = "{count:,} earlier commands and their outputs, not shown here"


def command_text(call, whole=False):
    """A call as the judge reads it: its tool's summary (a command, a file
    tool's path and content, cut as an ask shows it unless `whole`;
    core/tools.py), anything else its raw arguments, and what it crosses."""
    command = tools.summary(call.name, call.args, call.arguments, whole)
    crossing = tools.crossing(call.name, call.args)
    if crossing is not None:
        reason = str(crossing["why"] or "").strip() or "none given"
        command += CROSSING.format(tools.as_clause(crossing["fact"]), reason)
    # A call past a soft cap (jobs/hooks.py); the history has no notes
    limit = getattr(call, "notes", {}).get("limit")
    if limit:
        command += PAST_LIMIT.format(limit["reason"])
    return command


def notice_text(record):
    """A notice as the judge reads it: what happened, never the output it
    quotes, which is command output: its `summary` where its sender gives
    one (a job's notices), else its text."""
    text = record.get("summary") or record["text"]
    return text.strip().removeprefix("[").removesuffix("]").strip()


def clip(text, chars):
    """Its first and last characters, when longer than `chars`."""
    if len(text) <= chars:
        return text
    half = chars // 2
    left = len(text) - 2 * half
    return f"{text[:half]}\n[{left} characters left out]\n{text[-half:]}"


def view(records, output_chars=None, most_bytes=None, goal=()):
    """The judge sees what users said, ZERO's notices, and the commands with
    recorded execution, each with what it came to as its tool says it from
    its result's fields (a job's id with the command that became it). `goal`:
    the user's words in the session tree's other sessions, seen first. With
    `output_chars`, each command is followed by what it printed, cut to that
    many characters. With `most_bytes`, the oldest commands are left out
    until the entries fit in that many bytes of JSON lines, one entry saying
    how many."""
    entries = [{"user": text} for text in goal]
    for record in records:
        if record["type"] == "user" and log_history.is_notice(record):
            entries.append({"notice": notice_text(record)})
        elif record["type"] == "user":
            entries.append({"user": record["text"]})
        elif record["type"] == "tool" and record["ran"]:
            call = SimpleNamespace(
                name=record["tool"], arguments=record["arguments"],
                args=json.loads(record["arguments"]),
            )
            text = command_text(call)
            came_to = tools.result_summary(record["tool"], record.get("fields"))
            if came_to:
                text += f" ({came_to})"
            entries.append({"ran": text})
            if output_chars is not None:
                output = clip(record.get("result") or "", output_chars)
                entries.append({"output": output})
    if most_bytes is None:
        return entries
    return fitted(entries, most_bytes)


def entry_bytes(entry):
    return len(json.dumps(entry, ensure_ascii=False).encode("utf-8")) + 1


def fitted(entries, most_bytes):
    """`entries` in `most_bytes`: the oldest commands and outputs left out,
    every user message and notice kept."""
    size = sum(entry_bytes(e) for e in entries)
    if size <= most_bytes:
        return entries
    # Room for the line that says how many are left out
    room = most_bytes - entry_bytes({"left_out": LEFT_OUT.format(count=len(entries))})
    kept, count, first = [], 0, None
    for entry in entries:
        command = "ran" in entry or "output" in entry
        if command and size > room:
            size -= entry_bytes(entry)
            count += "ran" in entry
            first = len(kept) if first is None else first
            continue
        kept.append(entry)
    if first is not None:
        kept.insert(first, {"left_out": LEFT_OUT.format(count=count)})
    return kept


class JudgeApproval(Hook):
    """The auto modes' approver: the judge decides, and the user is never
    asked."""

    def __init__(self, judge):
        self.judge = judge

    def turn_start(self, turn):
        """Counted from the open turn's records, so a turn that comes back
        after a restart keeps its count (empty for a new turn)."""
        self._refusal_streak = self._refusals = 0
        for record in log_history.unfinished_turn(turn.session.log.branch()):
            if record["type"] != "tool":
                continue
            if record.get("notes", {}).get("judge_refused"):
                self._refusal_streak += 1
                self._refusals += 1
            elif record.get("ran"):
                self._refusal_streak = 0

    def approve(self, call):
        if not ask_of(call):
            return None
        session = call.turn.session
        records, goal = session.log.branch(), ()
        tree = session.tool_context.get("tree_user_messages")
        if tree is not None:
            goal, own_from = tree()
            records = [r for r in records if r["seq"] > own_from]
        history = view(records, self.judge.output_chars, self.judge.view_bytes,
                       goal)
        verdict = self._verdict(
            call.turn.control, history, command_text(call, whole=True),
            call.notes.get("offer_text"),
        )
        call.notes["judge"] = verdict
        decision = verdict["decision"]
        # "session" runs it as an allow; the grants read it
        # from the verdict in the log
        if decision in ("allow", "session"):
            if verdict["side_effects"]:
                effects = verdict["side_effects"].rstrip(".")
                call.remarks.append(SIDE_EFFECTS.format(effects=effects))
            return None
        self._refused(call)
        if decision == "deny":
            return Deny(DENIED.format(reason=verdict["reason"].rstrip(".")))
        return Deny(NO_VERDICT.format(error=verdict["error"]))

    def after_tool(self, call):
        if call.ran:
            self._refusal_streak = 0

    def _verdict(self, control, history, command, offer=None):
        """The judge's verdict. With a server's control, a stop returns at once:
        the judge's streamed call checks it, as the model's does (core/llm.py,
        complete), and the verdict never comes."""
        check = None if control is None else control.check
        return self.judge.decide(history, command, check=check, offer=offer)

    def step_end(self, step):
        if self._refusal_streak >= MAX_REFUSAL_STREAK:
            return Stop(
                "judge_refusals",
                f"{self._refusal_streak} in a row, {self._refusals} this turn",
            )

    def _refused(self, call):
        """A no: the judge's deny, or no verdict. Noted on the call, so
        turn_start can count it again from the log."""
        call.notes["judge_refused"] = True
        self._refusal_streak += 1
        self._refusals += 1
