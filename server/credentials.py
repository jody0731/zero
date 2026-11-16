"""Ephemeral credentials for commands using the scoped app endpoint."""

import hmac
import secrets
import threading
from dataclasses import dataclass, field


@dataclass(frozen=True)
class WorkflowAccess:
    socket: object
    token: str = field(repr=False)


class WorkflowCredentials:
    """Separate from the user's app token; revoked at server restart."""

    def __init__(self, socket):
        self.socket = socket
        self.lock = threading.Lock()
        self.sessions = {}

    def for_session(self, sid):
        with self.lock:
            if sid not in self.sessions:
                self.sessions[sid] = WorkflowAccess(
                    self.socket, secrets.token_urlsafe(32))
            return self.sessions[sid]

    def owner(self, token):
        if isinstance(token, str) and token:
            with self.lock:
                for sid, access in self.sessions.items():
                    if hmac.compare_digest(token.encode(), access.token.encode()):
                        return sid
        raise PermissionError("a valid workflow credential is required")

    def close(self):
        with self.lock:
            self.sessions.clear()
