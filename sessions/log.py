"""A session's history: one JSON record per line, append-only, never edited.

Each record has seq (its line number), time and type. Every append reaches the
disk (fsync) before the caller goes on, so what the log says happened did. A
crash mid-write can leave half a line at the end: reading leaves it out, and the
next append cuts it off first so the file stays whole. `on_append` hears each
record once it is on disk: the server hands it to the session's watchers. A new
session's log is named when it is made and claimed when its first turn starts
(fresh_log).
"""

import json
import os
import secrets
from datetime import datetime
from pathlib import Path


class Log:
    def __init__(self, path, lock=None, on_append=None):
        self.path = path
        self.lock = lock
        self.on_append = on_append
        # Whole records so far; counted on the first append
        self.count = None

    def records(self):
        """Every newline-terminated record; broken whole lines are errors."""
        if not self.path.exists():
            return []
        lines = self.path.read_bytes().split(b"\n")[:-1]
        records = []
        for n, line in enumerate(lines, 1):
            if not line:
                continue
            try:
                records.append(json.loads(line.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError(f"broken record at {self.path}:{n}") from None
        return records

    def append(self, type, **fields):
        """Write one record to disk and return it."""
        self.check_lock()
        if self.count is None:
            self.count = self._repair()
        record = {
            "seq": self.count + 1,
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "type": type,
            **fields,
        }
        new = not self.path.exists()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if new:
            # A new file's name lives in its folder, so that reaches the disk too
            folder = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
        self.count += 1
        if self.on_append is not None:
            self.on_append(record)
        return record

    def check_lock(self):
        if self.lock is not None and not self.lock.held:
            raise RuntimeError("the session lock must be held before writing its log")

    def _repair(self):
        """Cut off a broken last line; return how many whole records are left."""
        if not self.path.exists():
            return 0
        data = self.path.read_bytes()
        whole = data[: data.rfind(b"\n") + 1]
        if len(whole) != len(data):
            with open(self.path, "r+b") as f:
                f.truncate(len(whole))
                f.flush()
                os.fsync(f.fileno())
        return len(self.records())


class _FreshLog(Log):
    """Claim a new file exclusively when its first turn actually starts."""

    def __init__(self, path, model):
        super().__init__(path)
        self.model = model
        self.claimed = False

    def records(self):
        return super().records() if self.claimed else []

    def append(self, type, **fields):
        new = not self.claimed
        while not self.claimed:
            try:
                with open(self.path, "x", encoding="utf-8"):
                    pass
                self.claimed = True
            except FileExistsError:
                self.path = _log_path(self.path.parent, self.model)
        record = super().append(type, **fields)
        if new:
            # Log sees an existing file, so sync its newly claimed name here
            folder = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
        return record


def _log_path(folder, model):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = model.replace(":", "_").replace("/", "_")
    return Path(folder) / f"{stamp}-{safe}-{secrets.token_hex(2)}.jsonl"


def fresh_log(folder, model):
    """Pick a name; exclusive creation claims it when the first turn starts."""
    return _FreshLog(_log_path(folder, model), model)
