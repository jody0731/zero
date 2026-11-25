"""One session's live state, from its events: whether a turn runs, what it
does and since when, the open ask, the reply streaming in, a failed model
call's retry, what a stop gave back, the latest answer and the context's
meter (the server's `context` events); and sending it a
line (a new turn, a steer or a follow-up, or the reply to its ask). Every face
borrows it; what its keys do is the face's own (apps/tui/controller.py).
"""

import threading
import time

from client.render import command_of
from protocol import entries


class SessionState:
    def __init__(self, connection, session=None, mode=None, model=None,
                 workspace=None):
        """`mode` and `model` None: the server's defaults. `workspace`: the folder
        a new session gets, made when the first line is sent."""
        self.connection = connection
        self.session = session
        self.mode = mode
        self.model = model
        self.workspace = workspace
        # A new session's thinking effort; None: the server's default
        self.thinking_effort = None
        # A new session's utility service overrides; None: the server's default
        self.search_service = None
        self.stt_service = None
        self.tts_service = None
        self.image_service = None
        self.video_service = None
        self.auto_speak = None
        self.running = False
        # The open ask's `sessions/ask` record, from the log
        self.ask = None
        # What the turn is doing and since when (a monotonic time): thinking,
        # or running a command
        self.activity = None
        self.since = None
        # The `sessions/retry` record of a model call that failed, until a reply
        self.retry = None
        # The reply streaming in (the server's `draft`: step, text, reasoning,
        # calls), until the next record replaces it
        self.draft = None
        # Lines and attachment ids a stop gave back, for the input
        self.withdrawn = []
        self.withdrawn_attachments = []
        # The latest turn's answer as its Markdown, for /copy
        self.last_reply = None
        # The context's meter: tokens, line, cached_share (protocol's
        # CONTEXT_METER); None until the server or a call says
        self.context = None
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
                mine_src = getattr(self.connection, "source", None)
                for row in event["withdrawn"]:
                    if row.get("source") != mine_src:
                        continue
                    if row.get("text"):
                        self.withdrawn.append(row["text"])
                    for aid in row.get("attachments") or ():
                        if aid not in self.withdrawn_attachments:
                            self.withdrawn_attachments.append(aid)
                return
            if "context" in event:
                self.context = event["context"]
                return
            if "settled" in event:
                self.running = False
                self.activity = None
                self.retry = None
                self.draft = None
                return
            record = event.get("record") or {}
            if record:
                # Any record ends the draft: the reply's own, a retry, a stop
                self.draft = None
                if self.activity == "writing":
                    self.doing("thinking")
            kind = entries.entry_type(record)
            fields = entries.fields_of(record)
            if kind == "sessions/retry":
                self.retry = record
                return
            if (
                kind in ("chat/assistant_reply", "sessions/turn_end", "sessions/ask")
                or (kind in entries.NOTE_TYPES and fields.get("kind") == "fallback")
            ):
                self.retry = None
            # A note that the turn resumed after a restart means it runs again
            if entries.is_turn_input(record) or kind == "tools/result" or (
                kind in entries.NOTE_TYPES and fields.get("kind") == "restarted"
            ):
                self.running = True
                self.doing("thinking")
            elif kind == "chat/assistant_reply":
                calls = entries.tool_calls(record)
                if calls:
                    self.doing("running", command_of(
                        calls[0], record.get("call_summaries")))
            elif kind == "sessions/turn_end":
                if fields.get("answer"):
                    self.last_reply = fields["answer"]
                self.running = False
                self.ask = None
                self.activity = None
            elif kind == "sessions/ask":
                # Parked: nothing runs until the reply
                self.ask = record
                self.running = False
                self.activity = None
            elif kind == "sessions/answer" and self.ask is not None:
                if entries.ask_id(record) == entries.ask_id(self.ask):
                    self.ask = None
                    self.running = True
                    self.doing("thinking")

    def doing(self, what, command=None):
        self.activity = what if command is None else (what, command)
        self.since = time.monotonic()

    def line(self, text, follow_up=False, choice=None, reply_to=None, *,
             check=None, attachments=None):
        """Send a line: a new turn when idle, a steer mid-turn (or a follow-up,
        after the turn's answer), and the reply to the open ask while one
        waits, marked with its id, and with `choice` when it was picked at
        the box. `check` validates the same options the connection will send."""
        att_list = list(attachments) if attachments else None
        if not text.strip() and not att_list:
            return None
        with self.lock:
            options = {
                "session": self.session, "mode": self.mode, "model": self.model,
                "reply_to": reply_to, "workspace": self.workspace,
            }
            if self.session is None:
                if self.thinking_effort is not None:
                    options["thinking_effort"] = self.thinking_effort
                for name in (
                    "search_service", "stt_service", "tts_service",
                    "image_service", "video_service", "auto_speak",
                ):
                    value = getattr(self, name, None)
                    if value is not None:
                        options[name] = value
            if follow_up:
                options["when_busy"] = "queue"
            if choice is not None and reply_to is not None:
                options["choice"] = choice
            if att_list:
                options["attachments"] = att_list
        # Sent outside the lock: the follower's events go on meanwhile
        if check is not None:
            check(options)
        reply = self.connection.send(text, **options)
        with self.lock:
            self.session = reply["session"]
            if not self.running:
                self.doing("thinking")
            self.running = True
        return reply

    def take_withdrawn(self):
        with self.lock:
            lines, self.withdrawn = self.withdrawn, []
            return lines

    def take_withdrawn_attachments(self):
        with self.lock:
            ids, self.withdrawn_attachments = self.withdrawn_attachments, []
            return ids
