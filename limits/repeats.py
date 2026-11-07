"""A repeated call isn't run: the model is pointed at the earlier result."""

import time

from core import tools
from core.hooks import Deny, Hook

# Seconds after which the same call runs again: the world it looks at may have
# changed (a download grew, a service came up)
REPEAT_AFTER_SECONDS = 30


class RepeatGuard(Hook):
    """A repeated call isn't run; the model is pointed at the earlier result.

    It never ends the turn as stuck: the model always gets the chance to change
    course. A call counts once it
    has its result, except one the user's reply cancelled: the model may make it
    again, and the user is asked again. It starts fresh at every turn
    start, a resume after a restart or a parked turn coming back included, and
    a call REPEAT_AFTER_SECONDS seconds old may run again: the world may have
    changed meanwhile, so looking again is fair. A tool whose answer changes on
    its own (one that declares `changing`: a running job's output) is never
    held back.
    """

    def turn_start(self, turn):
        # (tool, normalised arguments) -> the step it first ran at
        self._seen = {}

    def before_tool(self, call):
        # A call someone already approved runs again when made again
        if tools.changing(call.name) or call.notes.get("granted") is not None:
            return None
        if call.key not in self._seen:
            return None
        step, when = self._seen[call.key]
        seconds = time.monotonic() - when
        if seconds >= REPEAT_AFTER_SECONDS:
            return None
        return Deny(
            f"Not run: the same call ran at step {step}, {seconds:.0f} s ago; its "
            "result is above."
        )

    def after_tool(self, call):
        if not call.error and not call.notes.get("cancelled"):
            self._seen[call.key] = (call.step.number, time.monotonic())
