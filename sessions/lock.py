"""One writer per session, held by the kernel even across a process's death."""

import fcntl
import os


class Busy(Exception):
    """Another writer holds this session's lock."""


class SessionLock:
    def __init__(self, path):
        self.path = path
        self.file = None

    @property
    def held(self):
        return self.file is not None

    def acquire(self):
        if self.held:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(self.path, "a+")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.seek(0)
            pid = stream.read().strip() or "?"
            stream.close()
            raise Busy(f"Session is in use by another ZERO host (pid {pid})") from None
        try:
            stream.seek(0)
            stream.truncate()
            stream.write(str(os.getpid()))
            stream.flush()
        except BaseException:
            stream.close()
            raise
        self.file = stream

    def release(self):
        if self.file is not None:
            self.file.close()
            self.file = None
