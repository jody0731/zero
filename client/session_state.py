"""One session's live state, from its events: whether a turn runs, what it
does and since when, the open ask, the reply streaming in, a failed model
call's retry, what a stop gave back and the latest answer; and sending it a
line (a new turn, a steer or a follow-up, or the reply to its ask). Every face
borrows it; what its keys do is the face's own (apps/tui/controller.py).
"""

import threading
import time

from client.render import command_of


class SessionState:
    def __init__(self, connection, session=None, mode=None, model=None,
                 workspace=None):
        """`mode` and `model` None: the host's defaults. `workspace`: the folder
        a new session gets, made when the first line is sent."""
        self.connection = connection
        self.session = session
        self.mode = mode
        self.model = model
        self.workspace = workspace
        self.running = False
        # The open ask's `ask` record, from the log
        self.ask = None
        # What the turn is doing and since when (a monotonic time): thinking,
        # or running a command
        self.activity = None
        self.since = None
        # The `retry` record of a model call that failed, until a reply
        self.retry = None
        # The reply streaming in (the host's `draft`: step, text, reasoning,
        # calls), until the next record replaces it
        self.draft = None
        # Lines a stop gave back, for the input
        self.withdrawn = []
        # The latest turn's answer as its Markdown, for /copy
        self.last_reply = None
        self.lock = threading.RLock()

    def event(self, event):
        with self.lock:
            if "draft" in event:
                draft = event["draft"]
                writing = bool(draft.get("text") or draft.get("calls"))
                if writing and self.activity != "writing":
                    self.doing("writing")
                self.draft = draft
                self.running = True
                return
            if "withdrawn" in event:
                mine = [m["text"] for m in event["withdrawn"]
                        if m.get("source") == getattr(self.connection, "source", None)]
                self.withdrawn.extend(mine)
                return
            if "settled" in event:
                self.running = False
                self.activity = None
                self.retry = None
                self.draft = None
                return
            record = event.get("record", {})
            if record:
                # Any record ends the draft: the reply's own, a retry, a stop
                self.draft = None
                if self.activity == "writing":
                    self.doing("thinking")
            kind = record.get("type")
            if kind == "retry":
                self.retry = record
                return
            if kind in ("assistant", "turn_end", "ask"):
                self.retry = None
            # A note that the turn resumed after a restart means it runs again
            if kind in ("user", "tool") or (
                kind == "note" and record.get("kind") == "restarted"
            ):
                self.running = True
                self.doing("thinking")
            elif kind == "assistant":
                calls = record.get("message", {}).get("tool_calls") or []
                if calls:
                    self.doing("running", command_of(calls[0]))
            elif kind == "turn_end":
                if record.get("answer"):
                    self.last_reply = record["answer"]
                self.running = False
                self.ask = None
                self.activity = None
            elif kind == "ask":
                # Parked: nothing runs until the reply
                self.ask = record
                self.running = False
                self.activity = None
            elif kind == "answer" and self.ask is not None:
                if record["ask"] == self.ask["ask"]:
                    self.ask = None
                    self.running = True
                    self.doing("thinking")

    def doing(self, what, command=None):
        self.activity = what if command is None else (what, command)
        self.since = time.monotonic()

    def line(self, text, follow_up=False, choice=None):
        """Send a line: a new turn when idle, a steer mid-turn (or a follow-up,
        after the turn's answer), and the reply to the open ask while one
        waits, marked with its id, and with `choice` when it was picked at
        the box."""
        with self.lock:
            if not text.strip():
                return None
            reply_to = self.ask["ask"] if self.ask is not None else None
            options = {
                "session": self.session, "mode": self.mode, "model": self.model,
                "reply_to": reply_to, "workspace": self.workspace,
            }
            if follow_up:
                options["when_busy"] = "queue"
            if choice is not None and reply_to is not None:
                options["choice"] = choice
            reply = self.connection.send(text, **options)
            self.session = reply["session"]
            if not self.running:
                self.doing("thinking")
            self.running = True
            return reply

    def take_withdrawn(self):
        with self.lock:
            lines, self.withdrawn = self.withdrawn, []
            return lines
