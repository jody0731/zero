"""A subagent's use against the estimate it was started with, checked as it
works: past it, its parent gets one notice (service.py writes it), and the
work goes on. Each subagent above it is checked too, since a parent's use
counts its subagents'. Nothing stops at an estimate (principle 3).
"""

import time

from core.hooks import Hook

# How often a working subagent's use is read again: often enough to tell
# its parent within a minute or so, seldom enough that reading its logs
# costs nothing a step would notice
CHECK_SECONDS = 30


class PastEstimate(Hook):
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._checked_at = None

    def step_end(self, step):
        found = step.turn.session.tool_context.get("session_subagents")
        if found is None:
            return None
        now = self.clock()
        if self._checked_at is not None and now - self._checked_at < CHECK_SECONDS:
            return None
        self._checked_at = now
        found.check_estimates()
        return None
