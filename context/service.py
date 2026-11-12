"""The context component's share of the server: a deleted session's results
folder (the whole outputs and notices kept out of its context) goes to the
trash with its log, and the line the context meter shows is its setting."""

from context.results import folder_of
from server.services import Service


class ContextService(Service):
    name = "context"

    def session_files(self, sid):
        """The session's results folder, where one was made."""
        return [folder_of(self.runtime.paths.sessions / f"{sid}.jsonl")]

    def meter_line(self):
        """compact_at_tokens as it stands: the line a session's context is
        kept under (room.py), which the user's meter shows."""
        return self.runtime.setting("compact_at_tokens")
