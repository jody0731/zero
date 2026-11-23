"""The context component's share of the server: compaction requests, a
deleted session's results folder, and the line the context meter shows."""

import threading

from components.context import compaction
from components.context.results import folder_of
from server.services import Refused, Service


class ContextService(Service):
    name = "context"

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}

    def messages(self):
        return {"session/compact": self.handle_compact}

    def handle_compact(self, request):
        sid = request.get("session")
        if not isinstance(sid, str):
            raise Refused("session/compact needs a session string")
        words = request.get("instructions")
        if words is not None and not isinstance(words, str):
            raise Refused("session/compact's instructions must be a string")
        return {"ok": True, **self.compact(sid, words)}

    def want_compaction(self, sid, by, instructions=None):
        """Record a mid-turn compaction request for `sid`."""
        with self._lock:
            self._pending[sid] = (by, instructions)

    def take_wanted(self, sid):
        """Return and clear any pending mid-turn compaction request for `sid`."""
        with self._lock:
            return self._pending.pop(sid, None)

    def session_built(self, sid, session):
        session.tool_context["take_wanted_compaction"] = (
            lambda: self.take_wanted(sid)
        )

    def session_deleted(self, sid):
        with self._lock:
            self._pending.pop(sid, None)

    def compact(self, sid, instructions=None):
        """Compact a session's context now if idle, or before the next step."""
        instructions = (instructions or "").strip() or None

        def when_busy(harness, row):
            if harness is None:
                raise Refused(
                    "its turn waits for an answer; a compaction "
                    "can be asked for once it runs again"
                )
            self.want_compaction(sid, "user", instructions)
            return {"compacting": "before_next_step"}

        def has_history():
            if not compaction.has_conversation(self.runtime.read_branch(sid)):
                raise Refused("the session has no conversation yet")
            # Clear any stale mid-turn request now that idle compaction starts.
            self.take_wanted(sid)

        result = self.runtime.run_on_session(
            sid, "compact",
            lambda session: session.compact("user", instructions=instructions),
            if_busy=when_busy, precheck=has_history,
        )
        return {"compacting": "now"} if result is None else result

    def session_files(self, sid):
        """The session's results folder, where one was made."""
        return [folder_of(self.runtime.paths.sessions / f"{sid}.jsonl")]

    def meter_line(self):
        """compact_at_tokens as it stands: the line a session's context is
        kept under (room.py), which the user's meter shows."""
        return self.runtime.setting("compact_at_tokens")
