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

    def __init__(self, socket, session_exists):
        self.socket = socket
        self.session_exists = session_exists
        self.lock = threading.Lock()
        self.secret = secrets.token_bytes(32)

    def for_session(self, sid):
        if not self.session_exists(sid):
            raise PermissionError("a live session is required")
        with self.lock:
            if self.secret is None:
                raise PermissionError("a live session is required")
            signature = hmac.digest(self.secret, sid.encode(), "sha256").hex()
            return WorkflowAccess(self.socket, f"{sid}.{signature}")

    def owner(self, token):
        verified = False
        if isinstance(token, str) and token:
            sid, separator, signature = token.rpartition(".")
            with self.lock:
                if self.secret is not None and separator and sid:
                    expected = hmac.digest(
                        self.secret, sid.encode(), "sha256").hex()
                    verified = hmac.compare_digest(
                        signature.encode(), expected.encode())
            if verified and self.session_exists(sid):
                with self.lock:
                    if self.secret is not None:
                        return sid
        raise PermissionError("a valid workflow credential is required")

    def close(self):
        with self.lock:
            self.secret = None
