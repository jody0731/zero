"""What a session's work came to, as one object: `zero session send --json`'s
and `zero mcp serve`'s zero_wait's.

A Result follows one submitted message to the end of the turn that took it.
Without a message id it follows the whole session until it settles. It reports
the outcome, answer, open question, token use, duration and background work
reported by the watch.
"""

from protocol import entries


class Result:
    def __init__(self, session, message_id=None):
        self.session = session
        self.message_id = message_id
        self.delivered = message_id is None
        self.last = None
        self.ask = None
        self.settled = False
        # What the services leave running once it settles, by kind: the
        # jobs' ids, the rounds' summary, the subagents' ids at work
        self.still_running = {}
        self.tokens = {"prompt": 0, "cached": 0, "completion": 0}
        self.seconds = 0.0

    def add(self, event):
        """Take one event; True once the session has settled."""
        record = event.get("record") or {}
        kind = entries.entry_type(record)
        fields = entries.fields_of(record)
        if self.message_id:
            if any(row["id"] == self.message_id for row in event.get("withdrawn", ())):
                self.last = {"outcome": "withdrawn"}
                self.settled = True
                return True
            if record and self.last:
                # A new turn can start before the session as a whole settles.
                self.settled = entries.is_turn_start(record)
                return self.settled
            if fields.get("inbox") == self.message_id:
                self.delivered = True
            if record and not self.delivered:
                return False
        if kind == "chat/assistant_reply":
            for name in self.tokens:
                self.tokens[name] += fields.get(f"{name}_tokens") or 0
        elif kind == "sessions/turn_end":
            self.last = record
            self.seconds += fields.get("seconds") or 0
        settled = event.get("settled")
        if settled is not None:
            self.ask = None if self.message_id and self.last else settled.get("ask")
            self.still_running = {
                kind: left for kind, left in settled.items()
                if kind != "ask" and left
            }
            self.settled = True
        return self.settled

    def _last_fields(self):
        if self.last is None:
            return {}
        return entries.fields_of(self.last) if "entry" in self.last else self.last

    def outcome(self):
        """The last turn's outcome; "waiting" while a question waits,
        "running" before it settles, None when nothing ran."""
        if not self.settled:
            return "running"
        if self.ask is not None:
            return "waiting"
        return None if self.last is None else self._last_fields()["outcome"]

    def exit_code(self):
        """The exit status: 0 answered, 1 not, 2 a question waits."""
        outcome = self.outcome()
        return 0 if outcome == "answered" else 2 if outcome == "waiting" else 1

    def as_json(self):
        answer = None
        last = self._last_fields()
        if self.outcome() not in ("waiting", "running") and last:
            answer = last.get("answer")
        return {
            "ok": True, "session": self.session, "outcome": self.outcome(),
            "answer": answer, "ask": self.ask,
            **({"reply": last["reply"]} if answer is not None
               and "reply" in last else {}),
            "still_running": dict(self.still_running),
            "tokens": dict(self.tokens),
            "seconds": round(self.seconds, 1),
        }


def still_running_line(still_running):
    """What still runs, as a line under the answer; '' for nothing."""
    parts = []
    jobs = still_running.get("jobs") or []
    if jobs:
        parts.append(f"{len(jobs)} job{'s' if len(jobs) > 1 else ''} "
                     f"({', '.join(jobs)})")
    subagents = still_running.get("subagents") or []
    if subagents:
        parts.append(f"{len(subagents)} subagent"
                     f"{'s' if len(subagents) > 1 else ''} "
                     f"({', '.join(subagents)})")
    if still_running.get("rounds"):
        parts.append(f"work in rounds ({still_running['rounds']})")
    return f"Still running: {'; '.join(parts)}" if parts else ""


def latest_turn(records):
    """The seq to watch from so a Result covers the session's latest turn:
    just before its latest message that wasn't a steer; 0 for none."""
    for record in reversed(records):
        if entries.is_turn_start(record):
            return record["seq"] - 1
    return 0
