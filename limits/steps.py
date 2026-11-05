"""The steps a turn takes before it asks to go on: a soft cap, decided as any
ask in the mode.

A cap is told to the model only when a call hits it, in the ask or the refusal
it gets, never in every prompt. The other soft cap, on running jobs, is the
jobs' own (jobs/plugin.py). The mode's approver answers the ask
(permissions/approval.py, judge/plugin.py); bypassPermissions loads no caps.
"""

from core import history
from core.hooks import Deny, Plugin, Stop


STEP_REFUSED = (
    "Not run: this turn reached its step limit and going on wasn't approved."
)


class StepLimit(Plugin):
    """Steps a turn may take before it asks to go on.

    The first call of a step past the limit is marked for the approver. A yes
    lets that call run and raises the limit by `grant` for this turn; a
    refusal (readOnly, nobody there, the user's no, the judge's deny) ends the
    turn `step_limit`, so a turn can't spin on refused calls. A reply in words
    instead of a yes reaches the model, and its next call asks again. A step
    with no call is an answer and needs no ask.
    """

    def __init__(self, max_steps, grant):
        self._base = max_steps
        self._grant = grant

    def turn_start(self, turn):
        """Rebuilt from the open turn's records: each call past the limit that
        ran was a yes, so a parked or restarted turn keeps its raised limit."""
        self._limit = self._base
        self._refused = False
        for record in history.pending(turn.session.log.records()):
            if record["type"] == "tool" and record.get("ran"):
                if record.get("notes", {}).get("step_limit"):
                    self._limit += self._grant

    def before_tool(self, call):
        """Only a step's first call asks: its yes raises the limit before the
        next call, a refusal denies the rest, and a reply in words closes them
        unstarted (core/loop.py)."""
        if call.step.number <= self._limit:
            return None
        if self._refused:
            return Deny(STEP_REFUSED)
        call.notes["step_limit"] = {
            "rule": "step_limit",
            "action": "ask",
            "reason": (
                f"this turn has taken {self._limit} steps, its limit; a yes lets "
                f"it go on for {self._grant} more"
            ),
            "about": (
                f"This turn has taken {self._limit} steps, its limit; a yes gives "
                f"it {self._grant} more."
            ),
        }
        return None

    def after_tool(self, call):
        if not call.notes.get("step_limit"):
            return
        if call.ran:
            self._limit += self._grant
        elif not call.notes.get("cancelled"):
            self._refused = True

    def step_end(self, step):
        if self._refused:
            return Stop(
                "step_limit", f"{self._limit} steps, going on wasn't approved"
            )
