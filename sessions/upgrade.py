"""Log formats, pure migrations, and replacing an old log with its original kept.

Format 1 is a log with no harness header: a session whose harness never built
writes one (its message and the crash), and its upgrade keeps the questions
and answers under the current header.

Format 2 carried in words what format 3 keeps as data beside them: a command's
exit code, time and job, where its output starts and whether it failed; a
job's notice without the output it quotes; a round's end. Its upgrade reads
those words once, here, and leaves every text as it was, so the model reads
the same.

Format 3 kept an ask's parts in its `why`, the rule's words before the
agent's reason; format 4 keeps the reason apart (an ask's `reason`, "" for
none), beside what the ask is `about`. Its upgrade cuts the rule's words off
an older ask's `why` once, here, and keeps the `why`, which the models read.

Only a runner upgrades files. Readers use migrate() in memory. A failed upgrade
leaves the old bytes in place, apart from the runtime's appended crash record.
"""

import json
import os
import re
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


# Format 2's words for what format 3 keeps as data: bash's answers
# (shell/output.py, jobs/tool.py), an error a tool raised (core/tools.py's
# run) and a call the user interrupted (core/loop.py)
EXIT_CODE = re.compile(r"Exit code: (-?\d+)\b")
STARTED = re.compile(r"Started in the background as (job-[0-9a-f]+)\.")
MOVED = re.compile(r"Still running after [\d.]+ s; moved to the background as "
                   r"(job-[0-9a-f]+)\.")
KILLED = "Timed out after "
WALL_TIME = re.compile(r"^Wall time: ([\d.]+) s$", re.MULTILINE)
OUTPUT = "\nOutput:\n"
NO_OUTPUT = "(no output)"
RAISED = re.compile(r"Error: [A-Z][A-Za-z]+: ")
INTERRUPTED = "Interrupted by the user while running"
# A job's notice (its source, jobs/tool.py) and where its output starts
JOB_SOURCE = "job"
LAST_OUTPUT = " Last output:"
# A round's end (rounds/hooks.py): its compaction's reason, and the occasion
# a fresh compaction now names
ROUND_REASON = "round"
ROUND_ENDED = "a round ended"


def bash_data(result):
    """What a format 2 bash answer said in its head, as format 3's data:
    `fields`, `failed` and `output_at`, those it holds."""
    started = STARTED.match(result)
    if started:
        return {"fields": {"job_id": started[1]}}
    head, marker, _ = result.partition(OUTPUT)
    exit_code, moved = EXIT_CODE.match(head), MOVED.match(head)
    if not marker:
        return {}
    if exit_code:
        fields = {"exit_code": int(exit_code[1])}
    elif moved:
        fields = {"job_id": moved[1], "timed_out": True}
    elif head.startswith(KILLED):
        fields = {"timed_out": True}
    else:
        return {}
    wall = WALL_TIME.search(head)
    if wall:
        fields["wall_seconds"] = float(wall[1])
    data = {"fields": fields}
    # Moved to the background, it goes on: not a failure
    if not moved and fields.get("exit_code") != 0:
        data["failed"] = True
    # With nothing printed, "(no output)" is the head's, and a note may follow
    at = len(head) + len(marker)
    rest = result[at + len(NO_OUTPUT):]
    if result[at:].startswith(NO_OUTPUT) and (not rest or rest.startswith("\n\n")):
        at += len(NO_OUTPUT)
    data["output_at"] = at
    return data


def tool_data(record):
    """A format 2 tool record's data, from its words."""
    if not record.get("ran"):
        return {}
    result = record.get("result") or ""
    if record.get("tool") == "bash":
        data = bash_data(result)
        if data:
            return data
    if RAISED.match(result) or result.startswith(INTERRUPTED):
        return {"failed": True}
    return {}


def from_2(records, header, time):
    """Give format 2's records the data their words held; every text stays."""
    first = records[0]
    result = [{**first, "format": 3,
               "upgraded_from": first.get("upgraded_from", 2)}]
    for record in records[1:]:
        record = dict(record)
        kind = record["type"]
        # A record a newer ZERO wrote under format 2 has its data already
        if kind == "tool" and not {"fields", "failed", "output_at"} & set(record):
            record.update(tool_data(record))
        elif (kind == "user" and record.get("source") == JOB_SOURCE
                and "summary" not in record
                and LAST_OUTPUT in (record.get("text") or "")):
            record["summary"] = record["text"].split(LAST_OUTPUT, 1)[0]
        elif (kind == "compaction" and record.get("reason") == ROUND_REASON
                and "fresh" not in record):
            record.update(fresh=True, occasion=ROUND_ENDED)
        result.append(record)
    return result


# Format 3's words before an ask's reason in its `why` (permissions'
# approval and asks, the judge's from when it could ask)
REASON_PREFIXES = ("rule: ", "it asks to run outside the sandbox: ",
                   "it fetches from a local address: ",
                   "the judge wants the user to decide: ")


def ask_reason(ask):
    """A format 3 ask's own reason: the one beside its `why`; else none
    where it says what it is about (a newer ZERO wrote any reason beside
    it); else its `why` without the rules' words before the reason."""
    if "reason" in ask:
        return ask["reason"]
    if "about" in ask:
        return ""
    why = ask.get("why") or ""
    for prefix in REASON_PREFIXES:
        if why.startswith(prefix):
            why = why[len(prefix):]
    return why.strip()


def from_3(records, header, time):
    """Give each ask its reason apart from its `why`; every text stays."""
    first = records[0]
    result = [{**first, "format": 4,
               "upgraded_from": first.get("upgraded_from", 3)}]
    for record in records[1:]:
        if record["type"] == "ask" and not isinstance(record.get("reason"), str):
            record = {**record, "reason": ask_reason(record)}
        result.append(record)
    return result


MIGRATIONS = {1: from_1, 2: from_2, 3: from_3}


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
