"""What a session's work came to, as one object: `zero session send --json`'s
and `zero mcp serve`'s zero_wait's.

A Result reads a session's watch, event by event, until it settles: the
outcome of its last turn (or "waiting" while a question waits), the answer,
the waiting question, the tokens of every model call it saw and the seconds
its turns took.
"""

# The ask's choices, as its box offers them
CHOICES = ("yes", "session", "saved", "no")


class Result:
    def __init__(self, session):
        self.session = session
        self.last = None
        self.ask = None
        self.settled = False
        self.tokens = {"prompt": 0, "cached": 0, "completion": 0}
        self.seconds = 0.0

    def add(self, event):
        """Take one event; True once the session has settled."""
        record = event.get("record", {})
        if record.get("type") == "assistant":
            for name in self.tokens:
                self.tokens[name] += record.get(f"{name}_tokens") or 0
        elif record.get("type") == "turn_end":
            self.last = record
            self.seconds += record.get("seconds") or 0
        settled = event.get("settled")
        if settled is not None:
            self.ask = settled.get("ask")
            self.settled = True
        return self.settled

    def outcome(self):
        """The last turn's outcome; "waiting" while a question waits,
        "running" before it settles, None when nothing ran."""
        if not self.settled:
            return "running"
        if self.ask is not None:
            return "waiting"
        return None if self.last is None else self.last["outcome"]

    def exit_code(self):
        """The exit status: 0 answered, 1 not, 2 a question waits."""
        outcome = self.outcome()
        return 0 if outcome == "answered" else 2 if outcome == "waiting" else 1

    def as_json(self):
        answer = None
        if self.outcome() not in ("waiting", "running") and self.last:
            answer = self.last.get("answer")
        return {
            "ok": True, "session": self.session, "outcome": self.outcome(),
            "answer": answer, "ask": self.ask, "tokens": dict(self.tokens),
            "seconds": round(self.seconds, 1),
        }


def latest_turn(records):
    """The seq to watch from so a Result covers the session's latest turn:
    just before its latest message that wasn't a steer; 0 for none."""
    for record in reversed(records):
        if record.get("type") == "user" and not record.get("steer"):
            return record["seq"] - 1
    return 0
