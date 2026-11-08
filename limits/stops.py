"""What ends a turn with its reason: malformed replies in a row. Long jobs
can rightly take hours: a turn's time and spend are its budget, the user's
(rounds/); a full context is compacted (context/)."""

from core.hooks import Hook, Stop


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
