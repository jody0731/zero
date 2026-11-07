"""The judge as the auto modes' approver: what it sees of a session (the
user's words, ZERO's notices, the commands that ran with their latest output)
and what its verdict does to a call. The judge itself is verdict.py.
"""

import json
import re
from types import SimpleNamespace

from core import history as log_history
from core import tools
from core.asking import ask_of
from core.hooks import Deny, Hook, Stop

# Claude Code's auto mode: this many refusals end the turn
MAX_REFUSAL_STREAK = 3
MAX_REFUSALS_PER_TURN = 20
# A command's result when it became a job (jobs/tool.py's started_text and
# moved_status), so the judge can tell which job a later `job` call means
JOB = re.compile(r"(Started in|moved to) (the background as job-[0-9a-f]+)\.")
DENIED = "Not run: the judge said no: {reason}."
NO_VERDICT = "Not run: the judge gave no verdict ({error})."
SIDE_EFFECTS = (
    "[The judge allowed this with side effects the goal can't avoid: {effects}.]"
)


# What a call crosses, in its tool's one sentence (its `crossing`), with the
# reason
CROSSING = "\n({}; reason: {})"
PAST_LIMIT = "\n({})"


def command_text(call):
    """A call as the judge reads it: its tool's summary (a command, a file
    tool's path and start; core/tools.py), anything else its raw arguments,
    and what it crosses."""
    command = tools.summary(call.name, call.args, call.arguments)
    crossing = tools.crossing(call.name, call.args)
    if crossing is not None:
        reason = str(crossing["why"] or "").strip() or "none given"
        command += CROSSING.format(tools.as_clause(crossing["fact"]), reason)
    # A call past a soft cap (limits/steps.py); the history has no notes
    for name in ("limit", "step_limit"):
        limit = getattr(call, "notes", {}).get(name)
        if limit:
            command += PAST_LIMIT.format(limit["reason"])
    return command


def notice_text(text):
    """A notice as the judge reads it: what happened, never the job's output,
    which is command output (a job's notices end with it)."""
    head = text.split("Last output:", 1)[0].strip()
    return head.removeprefix("[").removesuffix("]").strip()


def clip(text, chars):
    """Its first and last characters, when longer than `chars`."""
    if len(text) <= chars:
        return text
    half = chars // 2
    left = len(text) - 2 * half
    return f"{text[:half]}\n[{left} characters left out]\n{text[-half:]}"


def view(records, output_chars=None, outputs=0):
    """The judge sees what users said, ZERO's notices, and the commands with
    recorded execution, a job's id with the command that became it. With
    `output_chars`, the latest `outputs` commands are followed by what they
    printed, cut to that many characters."""
    ran = [r for r in records if r["type"] == "tool" and r["ran"]]
    shown = set()
    if output_chars is not None and outputs > 0:
        shown = {id(r) for r in ran[-outputs:]}
    entries = []
    for record in records:
        if record["type"] == "user" and log_history.is_notice(record):
            entries.append({"notice": notice_text(record["text"])})
        elif record["type"] == "user":
            entries.append({"user": record["text"]})
        elif record["type"] == "tool" and record["ran"]:
            call = SimpleNamespace(
                name=record["tool"], arguments=record["arguments"],
                args=json.loads(record["arguments"]),
            )
            text = command_text(call)
            job = JOB.search(record.get("result") or "")
            if job:
                text += f" ({job.group(1).lower()} {job.group(2)})"
            entries.append({"ran": text})
            if id(record) in shown:
                output = clip(record.get("result") or "", output_chars)
                entries.append({"output": output})
    return entries


class JudgeApproval(Hook):
    """The auto modes' approver: the judge decides, and the user is never
    asked."""

    def __init__(self, judge):
        self.judge = judge

    def turn_start(self, turn):
        """Counted from the open turn's records, so a turn that comes back
        after a restart keeps its count (empty for a new turn)."""
        self._refusal_streak = self._refusals = 0
        for record in log_history.unfinished_turn(turn.session.log.records()):
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
        history = view(
            call.turn.session.log.records(), self.judge.output_chars,
            self.judge.outputs,
        )
        verdict = self._verdict(
            call.turn.control, history, command_text(call),
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
        if (self._refusal_streak >= MAX_REFUSAL_STREAK
                or self._refusals >= MAX_REFUSALS_PER_TURN):
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
