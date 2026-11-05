"""What the file tools saw: each `read`, `write` and `edit` notes the content's
hash in its tool record (`file`: path, real, seen, or before and after), and the
log is the only record.

The stale check reads it (a file that changed since this session's tools last
saw it isn't changed), and so does the restart note (the files a cut-off
turn's tools touched that changed while it was cut off: ChangedFiles). No bytes
are kept: undo isn't the harness's, and ZERO makes its changes recoverable
itself.
"""

import hashlib
import os
import stat
from pathlib import Path

from core.hooks import Plugin


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FileMemory:
    """What a session's file tools saw, from its log. `log_of` gives the
    session's log as it stands (a fresh log's name is claimed only at its
    first turn)."""

    def __init__(self, log_of):
        self.log_of = log_of

    def seen(self, path):
        """The content's hash when this session's tools last read or wrote
        `path` (the path as the tools see it), or None for never."""
        return seen(self.log_of().records(), path)


class ChangedFiles(Plugin):
    """On a resume: the files the cut-off turn's tools read or wrote that
    changed while it was cut off, in the restart note."""

    def resumed(self, session, records):
        changed = changed_since(records)
        if not changed:
            return ""
        return (
            f" Changed since then, by something other than this turn: "
            f"{', '.join(changed)} (this turn had read or written them)."
        )


def changed_since(records):
    """The files (as the tools saw them) that `records`' tool calls read or
    wrote and that aren't as those calls left them now."""
    last = {}
    for record in records:
        if record["type"] == "tool" and record.get("ran"):
            note = (record.get("notes") or {}).get("file")
            if note and note.get("real"):
                last[note["real"]] = note
    return [
        note["path"] for real, note in last.items()
        if current(real) != (note.get("seen") or note.get("after"))
    ]


def seen(records, path):
    for record in reversed(records):
        if record["type"] == "tool" and record.get("ran"):
            note = (record.get("notes") or {}).get("file")
            if note and note.get("path") == path:
                return note.get("seen") or note.get("after")
    return None


def current(path):
    """A file's hash as it is now: None when missing, "other" for a link or
    anything not a regular file."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return "other"
    return sha(Path(path).read_bytes())
