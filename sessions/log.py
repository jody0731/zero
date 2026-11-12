"""A session's history: one JSON record per line, append-only, never edited.

Each record has seq (one more than the record above it; a fork's copied
records keep their parent's numbers), time and type; a rewind's also names
its parent, the record the conversation goes on from (core/history.py), so
the log is a tree and `branch()` its current branch. Every append reaches the
disk (fsync) before the caller goes on, so what the log says happened did. A
crash mid-write can leave half a line at the end: reading leaves it out, and the
next append cuts it off first so the file stays whole. `on_append` hears each
record once it is on disk: the server hands it to the session's watchers. A new
session's log is named when it is made and claimed when its first turn starts
(fresh_log). A Log keeps what it read in memory and later reads only what was
appended since, by itself or another writer, so a step's look at the log costs
what is new, not the whole log (design rule 12); the records it hands out are
the log's, read and never changed.
"""

import json
import os
import secrets
from datetime import datetime
from pathlib import Path

from core import history

# How much of a log's end is read at a time to find its last record
TAIL_BYTES = 64 * 1024


class Log:
    def __init__(self, path, lock=None, on_append=None):
        self.path = path
        self.lock = lock
        self.on_append = on_append
        # The newest whole record's seq, read on the first append: a fork's
        # log holds its parent's records with their numbers, so its seqs can
        # skip where the parent's branch did
        self.last_seq = None
        # What was read so far: the file (its inode), the bytes of its whole
        # lines, how many lines, the last of them, and their records
        self.kept = None

    def records(self):
        """Every newline-terminated record; broken whole lines are errors.
        Read from where the last read ended; the whole file again when it
        was replaced (a new inode), cut shorter (a half line repaired) or
        changed where it was read (its last line read isn't there)."""
        try:
            found = self.path.stat()
        except FileNotFoundError:
            self.kept = None
            return []
        empty = {"inode": found.st_ino, "size": 0, "lines": 0, "last": b"",
                 "records": []}
        kept = self.kept
        if (kept is None or kept["inode"] != found.st_ino
                or found.st_size < kept["size"]):
            kept = empty
        if found.st_size > kept["size"] or kept is not empty:
            with open(self.path, "rb") as f:
                f.seek(kept["size"] - len(kept["last"]))
                data = f.read()
            if not data.startswith(kept["last"]):
                kept = empty
                data = self.path.read_bytes()
            else:
                data = data[len(kept["last"]):]
            whole = data[: data.rfind(b"\n") + 1]
            added = []
            for n, line in enumerate(whole.split(b"\n")[:-1], kept["lines"] + 1):
                if not line:
                    continue
                try:
                    added.append(json.loads(line.decode("utf-8")))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise ValueError(f"broken record at {self.path}:{n}") from None
            if whole:
                kept = {
                    "inode": kept["inode"], "size": kept["size"] + len(whole),
                    "lines": kept["lines"] + whole.count(b"\n"),
                    "last": whole[whole.rfind(b"\n", 0, -1) + 1:],
                    "records": kept["records"] + added,
                }
        self.kept = kept
        return list(kept["records"])

    def first_record(self):
        """The log's first record, its header, read alone; None while empty."""
        if not self.path.exists():
            return None
        with open(self.path, "rb") as f:
            line = f.readline()
        if not line.endswith(b"\n"):
            return None
        return json.loads(line.decode("utf-8"))

    def last_record(self):
        """The log's last whole record, read from its end alone; None while
        it has none. What the start compares with the database's rows."""
        try:
            stream = open(self.path, "rb")
        except FileNotFoundError:
            return None
        with stream:
            end = stream.seek(0, os.SEEK_END)
            tail, at = b"", end
            while at > 0:
                at = max(0, at - TAIL_BYTES)
                stream.seek(at)
                tail = stream.read(end - at)
                whole = tail[: tail.rfind(b"\n") + 1]
                # Its last whole line starts after a newline, or at the top
                start = whole.rfind(b"\n", 0, -1)
                if whole and (start >= 0 or at == 0):
                    line = whole[start + 1:]
                    try:
                        record = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        record = None
                    if not isinstance(record, dict):
                        raise ValueError(f"broken last record at {self.path}")
                    return record
        return None

    def branch(self):
        """The records of the conversation's current branch, in order: all of
        them in a log no rewind has branched (core/history.py's `branch`)."""
        return history.branch(self.records())

    def append(self, type, **fields):
        """Write one record to disk and return it."""
        self.check_lock()
        if self.last_seq is None:
            self.last_seq = self._repair()
        record = {
            "seq": self.last_seq + 1,
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
        self.last_seq = record["seq"]
        if self.on_append is not None:
            self.on_append(record)
        return record

    def check_lock(self):
        if self.lock is not None and not self.lock.held:
            raise RuntimeError("the session lock must be held before writing its log")

    def _repair(self):
        """Cut off a broken last line; return the last whole record's seq (0
        for none)."""
        if not self.path.exists():
            return 0
        data = self.path.read_bytes()
        whole = data[: data.rfind(b"\n") + 1]
        if len(whole) != len(data):
            with open(self.path, "r+b") as f:
                f.truncate(len(whole))
                f.flush()
                os.fsync(f.fileno())
        records = self.records()
        return records[-1].get("seq", len(records)) if records else 0


def write_copy(path, records):
    """A new log at `path` holding `records` as they are, numbers kept (a
    fork's: its parent's branch up to where it was made); refused when the
    file exists. Reaches the disk before it returns, as an append does."""
    data = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    with open(path, "x", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    folder = os.open(Path(path).parent, os.O_RDONLY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


class _FreshLog(Log):
    """Claim a new file exclusively when its first turn actually starts."""

    def __init__(self, path, model):
        super().__init__(path)
        self.model = model
        self.claimed = False

    def records(self):
        return super().records() if self.claimed else []

    def first_record(self):
        return super().first_record() if self.claimed else None

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
