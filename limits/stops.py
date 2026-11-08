"""What ends a turn with its reason: agent time (the evals' hard cap) and
malformed replies in a row. Not in the chat's own limits: long jobs can
rightly take hours; the steps are a soft cap (steps.py); a full context is
compacted (context/)."""

from core.hooks import Hook, Stop


def hard_limits(limits):
    """The hard caps a session's limits set: TimeLimit by `max_seconds`, when
    there is one (the evals')."""
    if limits["max_seconds"] is None:
        return []
    return [TimeLimit(limits["max_seconds"])]


class TimeLimit(Hook):
    """Agent time per turn; time spent waiting for the user doesn't count."""

    def __init__(self, seconds):
        self._seconds = seconds

    def before_model(self, step):
        used = step.turn.agent_seconds()
        if used >= self._seconds:
            return Stop("time_limit", f"{used:.0f} s of agent time")


class BrokenReplyLimit(Hook):
    """End the turn after `limit` malformed or cut-off replies in a row."""

    def __init__(self, limit):
        self._limit = limit

    def turn_start(self, turn):
        self._streak = 0

    def after_model(self, step):
        self._streak = self._streak + 1 if step.malformed else 0

    def step_end(self, step):
        if self._streak >= self._limit:
            return Stop(
                "broken_replies",
                f"{self._streak} malformed or cut-off replies in a row"
            )
