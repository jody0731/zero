"""An append-only session log and fixed snapshots of its current branch."""

import errno
import json
import os
import secrets
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from threading import RLock

from core import timeline
from core.entries import Entry, has
from core.timeline import LogRecord
from sessions.entries import build_record, encode_record

# How much of a log's end is read at a time to find its last record
TAIL_BYTES = 64 * 1024
SYNC_FILE_RANGE_WRITE = getattr(os, "SYNC_FILE_RANGE_WRITE", 2)


def _load_sync_file_range():
    if sys.platform != "linux":
        return None
    if native := getattr(os, "sync_file_range", None):
        return native
    try:
        import ctypes

        syscall = ctypes.CDLL(None, use_errno=True).sync_file_range
    except (AttributeError, ImportError, OSError):
        return None
    syscall.argtypes = (ctypes.c_int, ctypes.c_int64, ctypes.c_int64, ctypes.c_uint)
    syscall.restype = ctypes.c_int

    def sync_file_range(fd, offset, length, flags):
        if syscall(fd, offset, length, flags):
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number))

    return sync_file_range


_sync_file_range = _load_sync_file_range()


def start_writeback(fd, offset, length):
    """Queue the appended bytes for writeback before any required fsync."""
    if _sync_file_range is None:
        return
    try:
        _sync_file_range(fd, offset, length, SYNC_FILE_RANGE_WRITE)
    except OSError as error:
        # A filesystem or sandbox may decline the optional hint.
        if error.errno not in (errno.ENOSYS, errno.ENOTSUP, errno.EPERM):
            raise


@dataclass(frozen=True, slots=True, eq=False)
class RecordSlice(Sequence[LogRecord]):
    """A fixed range in a list whose existing elements never change."""

    _source: list[LogRecord] = field(repr=False)
    _indices: range = field(repr=False)

    def __len__(self):
        return len(self._indices)

    def __getitem__(self, index):
        selected = self._indices[index]
        if isinstance(index, slice):
            return RecordSlice(self._source, selected)
        return self._source[selected]

    def __iter__(self):
        return (self._source[index] for index in self._indices)

    def __reversed__(self):
        return (self._source[index] for index in reversed(self._indices))

    def __eq__(self, other):
        if type(other) is not type(self):
            return False
        if self._source is other._source and self._indices == other._indices:
            return True
        return len(self) == len(other) and all(a == b for a, b in zip(self, other))

    __hash__ = None


EMPTY_RECORDS = RecordSlice([], range(0))


@dataclass(slots=True)
class _BranchState:
    settings: dict = field(default_factory=dict)
    running: dict | None = None
    meter: dict | None = None
    window_set: bool = False
    turn_start: int | None = None

    def take(self, record, index):
        entry = record.entry
        header = has(entry, "header")
        switch = timeline.is_switch(record)
        if (index == 0 and header) or switch:
            self.settings = timeline.renamed(entry.get("settings") or {})
        if index == 0 and header:
            self.running = timeline.starting_settings(record)
        if switch and self.running is not None:
            for name in timeline.SWITCHABLE:
                if name in self.settings and name not in timeline.CORE_SWITCHABLE:
                    self.running[name] = self.settings[name]
            self.running.update(timeline.renamed(entry.get("to") or {}))
        self.meter = timeline.metered(self.meter, record)
        self.window_set |= header or switch
        self.turn_start = timeline.open_turn_start(self.turn_start, index, record)

    def running_with(self):
        return {} if self.running is None else dict(self.running)

    def context_meter(self, compact_at):
        if not self.meter or not self.meter["tokens"]:
            return None
        meter = self.meter.copy()
        if self.window_set:
            meter["line"] = timeline.compact_line(meter["line"], compact_at)
        return meter


@dataclass(slots=True)
class _Cache:
    stamp: tuple | None = None
    bytes_read: int = 0
    lines_read: int = 0
    last_line: bytes = b""
    records: list[LogRecord] = field(default_factory=list)
    by_seq: dict[int, LogRecord] = field(default_factory=dict)
    branch: list[LogRecord] = field(init=False)
    branch_state: _BranchState = field(default_factory=_BranchState)
    has_parent: bool = False
    has_sequence_gap: bool = False

    def __post_init__(self):
        self.branch = self.records

    def append(self, record):
        previous = self.records[-1] if self.records else None
        follows_previous = (
            previous is None or timeline.parent_of(record) == previous.seq
        )
        starts_branch_graph = record.parent is not None and not self.has_parent
        self.has_parent |= record.parent is not None
        rebuild = self.has_parent and (
            not follows_previous or (starts_branch_graph and self.has_sequence_gap)
        )
        self.has_sequence_gap |= previous is not None and record.seq != previous.seq + 1
        self.records.append(record)
        self.by_seq[record.seq] = record
        if rebuild:
            # Existing slices still own the former branch's list.
            self.branch = timeline.branch_from(record, self.by_seq)
            self.branch_state = _BranchState()
            for index, row in enumerate(self.branch):
                self.branch_state.take(row, index)
        else:
            if self.branch is not self.records:
                self.branch.append(record)
            self.branch_state.take(record, len(self.branch) - 1)


def _stamp(stat):
    return (stat.st_dev, stat.st_ino), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def read_last_line(stream):
    """Return the last whole line by reading backwards from the file's end."""
    end = stream.seek(0, os.SEEK_END)
    at = end
    while at > 0:
        at = max(0, at - TAIL_BYTES)
        stream.seek(at)
        tail = stream.read(end - at)
        whole = tail[:tail.rfind(b"\n") + 1]
        start = whole.rfind(b"\n", 0, -1)
        if whole and (start >= 0 or at == 0):
            return whole[start + 1:]
    return b""


def tool_output(entry, transform, origin=None):
    """`entry` with `transform` applied to each part of it a tool wrote
    (effective origin "tool"), the rest shared as it was."""
    origin = entry.origin or origin
    if origin == "tool":
        return transform(entry)
    children = tuple(tool_output(child, transform, origin)
                     for child in entry.entries or ())
    if all(after is before for after, before in zip(children, entry.entries or ())):
        return entry
    return replace(entry, entries=children)


class Log:
    def __init__(self, path, lock=None, on_append=None, transform=None):
        """`transform(entry)`: applied to what a tool wrote before it is
        saved, a stopped call's result too (a server's redaction)."""
        self.path = Path(path)
        self.lock = lock
        self.on_append = on_append
        self.transform = transform
        self.last_seq = None
        self.kept = None
        self.needs_directory_sync = False
        self._cache_lock = RLock()
        self._append_lock = RLock()

    def _refresh(self):
        try:
            stamp = _stamp(self.path.stat())
        except FileNotFoundError:
            self.invalidate()
            return None
        kept = self.kept
        if kept is not None and kept.stamp == stamp:
            return kept
        try:
            with open(self.path, "rb") as stream:
                # The path could have changed between stat and open.
                stamp = _stamp(os.fstat(stream.fileno()))
                if kept is None or (
                    kept.stamp[0] != stamp[0] or stamp[1] <= kept.stamp[1]
                ):
                    kept = _Cache()
                start = kept.bytes_read - len(kept.last_line)
                stream.seek(start)
                data = stream.read(stamp[1] - start)
                if data.startswith(kept.last_line):
                    data = data[len(kept.last_line):]
                else:
                    # The file grew but the cached prefix's final line changed.
                    kept = _Cache()
                    stream.seek(0)
                    data = stream.read(stamp[1])
        except FileNotFoundError:
            self.invalidate()
            return None
        whole = data[:data.rfind(b"\n") + 1]
        added = []
        for number, line in enumerate(whole.split(b"\n")[:-1], kept.lines_read + 1):
            if line:
                try:
                    raw = json.loads(line.decode("utf-8"))
                    added.append(
                        timeline.read_record(raw, first=not (kept.records or added))
                    )
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise ValueError(f"broken record at {self.path}:{number}") from None
        try:
            for record in added:
                kept.append(record)
        except Exception:
            self.invalidate()
            raise
        if whole:
            kept.bytes_read += len(whole)
            kept.lines_read += whole.count(b"\n")
            kept.last_line = whole[whole.rfind(b"\n", 0, -1) + 1:]
        kept.stamp = stamp
        self.kept = kept
        return kept

    def records(self):
        """A snapshot of all whole records, including branches left behind."""
        with self._cache_lock:
            kept = self._refresh()
            return (RecordSlice(kept.records, range(len(kept.records)))
                    if kept is not None else EMPTY_RECORDS)

    def branch(self):
        """A snapshot of the present branch in conversation order."""
        with self._cache_lock:
            kept = self._refresh()
            return (RecordSlice(kept.branch, range(len(kept.branch)))
                    if kept is not None else EMPTY_RECORDS)

    def settings_now(self):
        """The active settings after the branch's last switch."""
        with self._cache_lock:
            kept = self._refresh()
            return kept.branch_state.settings.copy() if kept is not None else {}

    def running_with(self):
        """The active model, mode, workspace, effort and judge."""
        with self._cache_lock:
            kept = self._refresh()
            return kept.branch_state.running_with() if kept is not None else {}

    def context_meter(self, compact_at=None):
        """The active branch's token usage, context line and prompt cache share."""
        with self._cache_lock:
            kept = self._refresh()
            if kept is not None:
                return kept.branch_state.context_meter(compact_at)
        return None

    def unfinished_turn(self):
        """A snapshot of just the current open turn."""
        with self._cache_lock:
            kept = self._refresh()
            if kept is None or kept.branch_state.turn_start is None:
                return EMPTY_RECORDS
            return RecordSlice(
                kept.branch, range(kept.branch_state.turn_start, len(kept.branch)),
            )

    def _kept_if_current(self):
        kept = self.kept
        if kept is None:
            return None
        try:
            return kept if kept.stamp == _stamp(self.path.stat()) else None
        except FileNotFoundError:
            self.invalidate()
            return None

    def first_record(self):
        """Read the first record without loading the rest of the log."""
        with self._cache_lock:
            if (kept := self._kept_if_current()) is not None:
                return kept.records[0] if kept.records else None
            try:
                with open(self.path, "rb") as stream:
                    line = stream.readline()
            except FileNotFoundError:
                return None
            if line.endswith(b"\n"):
                raw = json.loads(line.decode("utf-8"))
                return timeline.read_record(raw, first=True)
        return None

    def last_record(self):
        """Read the last whole record without loading the branch."""
        with self._cache_lock:
            if (kept := self._kept_if_current()) is not None:
                return kept.records[-1] if kept.records else None
            try:
                stream = open(self.path, "rb")
            except FileNotFoundError:
                return None
            with stream:
                header = stream.readline()
                if not header.endswith(b"\n"):
                    return None
                raw_header = json.loads(header.decode("utf-8"))
                timeline.native_format(raw_header, first=True)
                line = read_last_line(stream)
                if not line:
                    return None
                try:
                    raw = (raw_header if line == header else
                           json.loads(line.decode("utf-8")))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise ValueError(f"broken last record at {self.path}") from None
                return timeline.read_record(raw, first=line == header)

    def append(self, kind, /, **fields):
        """Append a declared entry or build one from its fields."""
        self.check_lock()
        with self._append_lock:
            with self._cache_lock:
                try:
                    kept = self._repair()
                    last = (
                        kept.records[-1].seq
                        if kept is not None and kept.records else 0
                    )
                    time = None
                    if not isinstance(kind, Entry):
                        time = fields.pop("time", None)
                        fields.pop("seq", None)
                    record = build_record(
                        last + 1, kind, fields.pop("parent", None),
                        fields=fields, time=time, session=self.path.stem,
                    )
                    if self.transform is not None:
                        entry = tool_output(record.entry, self.transform)
                        if entry is not record.entry:
                            record = replace(record, entry=entry)
                    self._append_line(
                        record,
                        kept,
                        durable=has(record.entry, "durable_boundary"),
                    )
                except OSError:
                    # The write may have left a whole line or a partial line.
                    self.invalidate()
                    raise
            if self.on_append is not None:
                self.on_append(record)
            return record

    def _append_line(self, record, kept, *, durable):
        data = encode_record(record)
        if kept is None:
            self.needs_directory_sync = True
        with open(self.path, "ab") as stream:
            start = stream.tell()
            stream.write(data)
            stream.flush()
            fd = stream.fileno()
            start_writeback(fd, start, len(data))
            if self.needs_directory_sync or not start or durable:
                os.fsync(fd)
            stamp = _stamp(os.fstat(fd))
        if self.needs_directory_sync:
            sync_folder(self.path.parent)
            self.needs_directory_sync = False
        if kept is None and not start:
            kept = _Cache(stamp=stamp)
        if (kept is not None and kept.stamp[0] == stamp[0]
                and kept.bytes_read == start and stamp[1] == start + len(data)):
            try:
                kept.append(record)
            except Exception:
                self.invalidate()
                raise
            kept.stamp = stamp
            kept.bytes_read += len(data)
            kept.lines_read += 1
            kept.last_line = data
            self.kept = kept
        else:
            self.kept = None
        self.last_seq = record.seq

    def check_lock(self):
        if self.lock is not None and not self.lock.held:
            raise RuntimeError("the session lock must be held before writing its log")

    def invalidate(self):
        with self._cache_lock:
            self.kept = None
            self.last_seq = None

    def _repair(self):
        """Drop a torn final line before appending."""
        kept = self._refresh()
        if kept is not None and kept.bytes_read < kept.stamp[1]:
            with open(self.path, "r+b") as stream:
                stream.truncate(kept.bytes_read)
                stream.flush()
                os.fsync(stream.fileno())
            self.invalidate()
            return self._refresh()
        return kept


def write_copy(path, records):
    """Exclusively create a synced copy of the selected records, with seqs kept."""
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.copy-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            for record in records:
                stream.write(encode_record(record))
            stream.flush()
            os.fsync(stream.fileno())
        os.link(name, path)
    finally:
        Path(name).unlink()
    sync_folder(path.parent)


def sync_folder(path):
    folder = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


class _FreshLog(Log):
    """Claim a new file exclusively when its first turn starts."""

    def __init__(self, path, model):
        super().__init__(path)
        self.model = model
        self.claimed = False

    def _refresh(self):
        return super()._refresh() if self.claimed else None

    def first_record(self):
        with self._cache_lock:
            return super().first_record() if self.claimed else None

    def last_record(self):
        with self._cache_lock:
            return super().last_record() if self.claimed else None

    def append(self, kind, /, **fields):
        with self._cache_lock:
            while not self.claimed:
                try:
                    with open(self.path, "x", encoding="utf-8"):
                        pass
                    self.claimed = True
                    self.needs_directory_sync = True
                except FileExistsError:
                    self.path = _log_path(self.path.parent, self.model)
        return super().append(kind, **fields)


def _log_path(folder, model):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = model.replace(":", "_").replace("/", "_")
    return Path(folder) / f"{stamp}-{safe}-{secrets.token_hex(2)}.jsonl"


def fresh_log(folder, model):
    """Pick a name; exclusive creation claims it when the first turn starts."""
    return _FreshLog(_log_path(folder, model), model)
