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

Format 5 permits an ask before the first model call, with no tool call to
resume. Its upgrade changes only the header; existing records keep their text.

Only a runner upgrades files. Readers use migrate() in memory. A failed upgrade
leaves the old bytes in place, apart from the runtime's appended crash record.
"""

import json
import os
import re
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from core import attribution, entries, timeline
from core.llm import Draft
from core.timeline import LOG_FORMAT, OldLog
from sessions.entries import build_record, encode_record
from sessions.log import read_last_line, sync_folder

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


def from_4(records, header, time):
    """Format 5 can park before a model call; old records keep their text."""
    first, *rest = records
    return [{**first, "format": 5,
             "upgraded_from": first.get("upgraded_from", 4)}, *rest]


def from_5(records, header, time):
    """Format 6 separates questions from permission asks."""
    first, *rest = records
    return [{**first, "format": 6,
             "upgraded_from": first.get("upgraded_from", 5)}, *rest]


def from_6(records, header, time):
    """Format 7 adds private input asks whose values are never log records."""
    first, *rest = records
    return [{**first, "format": 7,
             "upgraded_from": first.get("upgraded_from", 6)}, *rest]


def read_plain_reply(reply):
    """The saved sections as Markdown for model replay."""
    next_steps = reply.get("next") or {}
    return "\n\n".join(text for text in (
        reply.get("answer"), reply.get("why"), next_steps.get("user"),
        next_steps.get("zero"),
    ) if text)


def without_reply_instructions(prompt):
    body, separator, paragraph = prompt.rpartition("\n\n")
    if not separator:
        paragraph = prompt
    if (paragraph.startswith("Your public reply is one JSON object in this form:")
            and paragraph.endswith("Internal handoffs have their own format.")):
        return body
    return prompt


def legacy_assistant(message, reply=None):
    """Bring saved text, calls and opaque continuation into their own nodes."""
    draft = Draft()
    draft.content = read_plain_reply(reply) if reply else message.get("content")
    draft.reasoning = message.get("reasoning_content")
    for index, call in enumerate(message.get("tool_calls") or ()):
        draft.calls[index] = {"id": call["id"], **call["function"]}
    state = message.get("provider_state")
    route = state.get("route") if isinstance(state, dict) else None
    reply = draft.entry(route=route)
    if state is None:
        return reply
    continuation = entries.make("provider_item", fields={
        "route": route, "kind": "legacy_provider_state", "item": state,
    })
    return replace(reply, entries=(*reply.entries, continuation))


def from_7(records, header, time):
    """Give flat records stable envelopes without changing their sequence."""
    result = []
    for index, original in enumerate(records, 1):
        record = {**original, "time": original.get("time") or time}
        if index == 1 and record["type"] == "session":
            record.update(format=LOG_FORMAT, upgraded_from=record.get(
                "upgraded_from", 7))
        if "system_prompt" in record:
            prompt = record["system_prompt"]
            record["system_prompt"] = without_reply_instructions(prompt)
        result.append(_legacy_record(record, time, index, migrate_reply=True))
    return result


MIGRATIONS = {1: from_1, 2: from_2, 3: from_3, 4: from_4, 5: from_5,
              6: from_6, 7: from_7}


def backup_path(path, source):
    """Where an upgraded log's original is kept: beside it, its format in
    its name."""
    return path.with_name(f"{path.stem}.format-{source}.jsonl")


def is_backup(path):
    """Whether a file in the sessions' folder is an upgraded log's original
    (backup_path), not a session's log."""
    return ".format-" in Path(path).name


def _legacy_record(line, time, seq, *, migrate_reply=False):
    kind = line["type"]
    seq = line.get("seq", seq)
    stamp = line.get("time") or time
    fields = {key: value for key, value in line.items()
              if key not in ("seq", "type", "parent", "time")}
    raw_settings = fields.get("settings")
    if isinstance(raw_settings, dict):
        judge = raw_settings.get("judge")
        if isinstance(judge, dict) and "judge_model" not in raw_settings:
            updated = dict(raw_settings)
            updated["judge_model"] = judge.get("model")
            if "effort" in judge and "judge_thinking_effort" not in updated:
                updated["judge_thinking_effort"] = judge.get("effort")
            fields["settings"] = updated
    # A read-only migration and a later disk upgrade agree on inherited IDs.
    entry_id = (fields.get("ask") if kind == "ask" else None) or f"record-{seq}"
    if kind == "compaction":
        children = []
        if "system_prompt" in fields:
            children.append(entries.make(
                "system_prompt", fields={"text": fields.pop("system_prompt")},
            ))
        if fields.get("summary"):
            children.append(entries.make(
                "text", origin="model", fields={"text": fields.pop("summary")},
            ))
        entry = entries.make(
            "compaction_handoff", id=entry_id, origin="system",
            created_at=stamp, fields=fields or None,
            entries=tuple(children) or None,
        )
        return build_record(seq, entry, line.get("parent"))
    if kind in ("compaction_failed", "nothing_to_compact"):
        entry = entries.make(
            kind, id=entry_id, origin="system",
            created_at=stamp, fields=fields or None,
        )
        return build_record(seq, entry, line.get("parent"))
    if kind == "assistant" and (message := fields.pop("message", None)) is not None:
        fields["entry"] = legacy_assistant(
            message, fields.get("reply") if migrate_reply else None,
        )
    return build_record(seq, kind, line.get("parent"), time=stamp,
                        fields=fields, entry_id=entry_id)


def read_record(line, *, time=None, seq=None):
    """Read a single native or legacy line without renumbering it."""
    try:
        return timeline.read_record(line)
    except OldLog as old:
        record = _legacy_record(line, time or timestamp(), seq or 1)
        if entries.has(record.entry, "header"):
            record = replace(record, log_format=old.found)
        return record


def _source_format(line):
    if line is None:
        return 1
    mark = timeline.marked_format(line, first=True)
    if mark is not None and mark < LOG_FORMAT:
        return mark
    if mark == LOG_FORMAT or "entry" in line:
        return timeline.log_format((timeline.read_record(line, first=True),))
    return mark or 1


def _headerless_record(line):
    """Recover the original format 1 facts from a partially started native log."""
    if "entry" not in line:
        return line
    record = timeline.read_record(line)
    entry = record.entry
    fields = dict(entry.fields or {})
    if timeline.is_turn_input(entry):
        kind = "user"
        fields["text"] = attribution.delivered_text(entry)
    elif entries.has(entry, "turn_end"):
        kind = "turn_end"
    else:
        raise ValueError(f"cannot migrate format 1 entry type {entry.type!r}")
    return {**fields, "type": kind, "seq": record.seq, "time": record.time}


def migrate(records, header=None, *, time):
    """Pure in-memory upgrades of legacy records, one version at a time."""
    if not records:
        return []
    source = _source_format(records[0])
    if source == LOG_FORMAT:
        return [timeline.read_record(line) for line in records]
    if source == 1:
        records = [_headerless_record(line) for line in records]
    while source < LOG_FORMAT:
        records = MIGRATIONS[source](records, header, time)
        source += 1
    return records


def _decode(path, line, number):
    try:
        raw = json.loads(line.decode("utf-8"))
        if isinstance(raw, dict):
            return raw
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    raise ValueError(f"broken record at {path}:{number}")


def _read_first(path):
    try:
        with open(path, "rb") as stream:
            first = stream.readline()
            number = 1
            while first == b"\n":
                first = stream.readline()
                number += 1
    except FileNotFoundError:
        return None
    return _decode(path, first, number) if first.endswith(b"\n") else None


def _read_raw(path, data):
    whole = data[:data.rfind(b"\n") + 1]
    records = []
    for number, line in enumerate(whole.split(b"\n")[:-1], 1):
        if line:
            raw = _decode(path, line, number)
            if not records:
                timeline.marked_format(raw, first=True)
            records.append(raw)
    return records


def file_format(path):
    """Read the original log's version without loading the rest of the file."""
    return _source_format(_read_first(path))


def read_header(log):
    """A read-only view of the first record, preserving its original version."""
    try:
        return log.first_record()
    except OldLog:
        line = _read_first(log.path)
        if line is not None:
            timeline.marked_format(line, first=True)
            return read_record(line)
        return None


def last_seq(log):
    """Probe the last complete record's original sequence without migrating."""
    try:
        last = log.last_record()
        return last.seq if last is not None else 0
    except OldLog:
        try:
            with open(log.path, "rb") as stream:
                first = stream.readline()
                if first.endswith(b"\n"):
                    timeline.marked_format(_decode(log.path, first, 1), first=True)
                last = read_last_line(stream)
        except FileNotFoundError:
            return 0
        return _decode(log.path, last, "last").get("seq", 0) if last else 0


def read_records(log, *, time=None):
    """Read native snapshots or migrate a legacy log in memory for a view."""
    try:
        records = log.records()
        if not records or timeline.log_format(records) == LOG_FORMAT:
            return records
    except OldLog:
        pass
    try:
        data = log.path.read_bytes()
    except FileNotFoundError:
        return ()
    return migrate(_read_raw(log.path, data), time=time or timestamp())


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
    try:
        original = log.path.read_bytes()
    except FileNotFoundError:
        return False
    records = _read_raw(log.path, original)
    source = _source_format(records[0] if records else None)
    if source == LOG_FORMAT:
        for raw in records[1:]:
            timeline.read_record(raw)
        return False
    stamp = timestamp()
    # A writer supplies a real header when an old log has no whole records.
    if not records:
        records = from_1(records, header, stamp)
    upgraded = migrate(records, header, time=stamp)
    data = b"".join(encode_record(record) for record in upgraded)
    temp = _temp(log.path.parent, log.path.name, data)
    try:
        backup = backup_path(log.path, source)
        _backup(backup, original)
        sync_folder(log.path.parent)
        os.replace(temp, log.path)
        log.invalidate()
        sync_folder(log.path.parent)
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
    fields = {"inbox": inbox, "outcome": "crashed", "error": error}
    stamp = timestamp()
    try:
        header = _read_first(log.path)
    except ValueError:
        header = None
    mark = timeline.marked_format(header, first=True) if header is not None else None
    seq = count + 1
    if (header is not None and "entry" in header
            and (mark is None or mark == LOG_FORMAT)):
        try:
            # A fork can have fewer lines than its last sequence number.
            whole = data[:data.rfind(b"\n") + 1]
            last = _decode(log.path, whole.splitlines()[-1], "last")
            if type(last.get("seq")) is int:
                seq = max(seq, last["seq"] + 1)
        except (ValueError, IndexError):
            pass
        ending = encode_record(build_record(seq, "turn_end", time=stamp, fields=fields))
    else:
        record = {"seq": seq, "time": stamp, "type": "turn_end", **fields}
        ending = (json.dumps(record, ensure_ascii=False) + "\n").encode()
    with open(log.path, "ab") as stream:
        stream.write(separator + ending)
        stream.flush()
        os.fsync(stream.fileno())
    log.invalidate()
