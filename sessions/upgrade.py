"""Log formats, pure migrations, and replacing an old log with its original kept.

Format 1 is a log with no harness header: a session whose harness never built
writes one (its message and the crash), and its upgrade keeps the questions
and answers under the current header.

Only a runner upgrades files. Readers use migrate() in memory. A failed upgrade
leaves the old bytes in place, apart from the runtime's appended crash record.
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from core.history import LOG_FORMAT, NewerLog, log_format

UPGRADED = (
    "[The turns below come from a log without a harness header: their questions "
    "and answers are kept, the commands they ran are not.]"
)


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def from_1(records, header, time):
    """Keep format 1's questions and endings, explicitly marking what was lost."""
    if header is None:
        old = next((r for r in records if r["type"] == "session"), {})
        header = {
            "model": old.get("model", ""), "mode": old.get("mode", ""),
            "settings": {}, "hooks": [],
            "system_prompt": old.get("system_prompt", ""),
        }
    result = [{
        **header, "type": "session", "format": 2, "upgraded_from": 1, "time": time,
    }]
    if any(r["type"] == "user" for r in records):
        result.append({"type": "note", "kind": "upgraded", "text": UPGRADED,
                       "time": time})
    user = None
    for record in records:
        kind = record["type"]
        if kind == "session":
            continue
        if kind == "user":
            if user is not None:
                raise ValueError("format 1 has a new user before the prior turn ended")
            if not isinstance(record["text"], str):
                raise ValueError("format 1 user text must be a string")
            user = record
            result.append(dict(record))
        elif kind == "turn_end":
            # A failed upgrade may have logged an ending before a user was written
            if user is None:
                continue
            if record.get("inbox") != user.get("inbox"):
                raise ValueError("format 1 ending belongs to a different inbox row")
            outcome = record["outcome"]
            if outcome == "answered":
                answer = record["answer"]
                if not isinstance(answer, str):
                    raise ValueError("format 1 answer must be a string")
                result.append({
                    "type": "assistant", "time": time, "step": 1,
                    "message": {"role": "assistant", "content": answer},
                    "finish_reason": "stop", "prompt_tokens": 0, "cached_tokens": 0,
                    "completion_tokens": 0, "retries": 0, "model_seconds": 0.0,
                    "upgraded": True,
                })
            else:
                result.append({
                    "type": "note", "time": time, "kind": "upgraded",
                    "text": f"[That turn ended without an answer ({outcome}).]",
                })
            result.append(dict(record))
            user = None
        else:
            raise ValueError(f"cannot migrate format 1 record type {kind!r}")
    return [{**r, "seq": n} for n, r in enumerate(result, 1)]


MIGRATIONS = {1: from_1}


def migrate(records, header=None, *, time):
    """Pure in-memory upgrades, one step at a time; no file or clock access."""
    if not records:
        return []
    source = log_format(records)
    while source < LOG_FORMAT:
        records = MIGRATIONS[source](records, header, time)
        source += 1
    return records


def _temp(folder, name, data):
    fd, name = tempfile.mkstemp(prefix=f".{name}.upgrade-", dir=folder)
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _backup(path, original):
    """Publish a complete copy exclusively; a torn copy never owns the name."""
    if path.exists():
        if path.read_bytes() != original:
            raise ValueError(f"upgrade backup differs from the original: {path}")
    else:
        temp = _temp(path.parent, path.name, original)
        try:
            try:
                os.link(temp, path)
            except FileExistsError:
                if path.read_bytes() != original:
                    raise ValueError(
                        f"upgrade backup differs from the original: {path}"
                    )
        finally:
            temp.unlink(missing_ok=True)
    with open(path, "rb") as stream:
        os.fsync(stream.fileno())


def upgrade_file(log, header):
    """Fsync the replacement and original copy, then atomically replace the log."""
    log.check_lock()
    records = log.records()
    source = log_format(records)
    if source > LOG_FORMAT:
        raise NewerLog(source)
    if not log.path.exists() or source == LOG_FORMAT:
        return False
    original = log.path.read_bytes()
    stamp = timestamp()
    # A writer supplies a real header when an old log has no whole records.
    if not records:
        records = from_1(records, header, stamp)
    upgraded = migrate(records, header, time=stamp)
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in upgraded).encode()
    temp = _temp(log.path.parent, log.path.name, data)
    try:
        backup = log.path.with_name(f"{log.path.stem}.format-{source}.jsonl")
        _backup(backup, original)
        os.replace(temp, log.path)
        log.last_seq = None
        folder = os.open(log.path.parent, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    finally:
        temp.unlink(missing_ok=True)
    return True


def record_failure(log, inbox, error):
    """Append the failure even when a whole broken line prevents Log reading it.

    Keep every original byte. Do not repair or replace a failed migration's log.
    """
    log.check_lock()
    data = log.path.read_bytes()
    separator = b"" if not data or data.endswith(b"\n") else b"\n"
    count = data.count(b"\n") + bool(separator)
    record = {
        "seq": count + 1, "time": timestamp(), "type": "turn_end",
        "inbox": inbox, "outcome": "crashed", "error": error,
    }
    ending = (json.dumps(record, ensure_ascii=False) + "\n").encode()
    with open(log.path, "ab") as stream:
        stream.write(separator + ending)
        stream.flush()
        os.fsync(stream.fileno())
    log.last_seq = None
