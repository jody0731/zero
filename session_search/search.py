"""Session search's three uses, over the index and the session list:

- `query` alone: the sessions whose records match, best first, each with
  how many of its records match and its best one; the session that asks
  after the others, since its own words match;
- `query` and `session`: that session's matching records, best first;
- `session` and `around`: the records near one, whole, read from the log,
  `count` on each side; `session` alone, its first records.

A record matches when it holds every word; when none does, the records
holding any of them are given instead, and the reply says which
(`matched_by`): a long query of a model's or a person's still finds its
nearest records.

A session's log keeps the parts of its conversation a rewind left behind
(core/history.py): a record found there is marked `left_behind`. A fork's
records copied from the session it came from aren't searched in the fork
(index.py): they are found in that session.

Each use returns the reply `session/search` gives (protocol/messages.py),
which the tool puts in words for ZERO (tool.py) and the command in lines
for a person. Every session the list holds is searched: running or done,
archived, the one asking; a deleted one isn't listed.
"""

import json

from core import history
from session_search.index import fts_query, named, searchable

# How many sessions, or records, a search gives when the caller names no
# count
COUNT = 10
# The records shown on each side of `around` when the caller names no count
EACH_SIDE = 5
# Where a session named alone is read from: its first record (a log's
# first, the session's settings, isn't searched, so the next comes)
FIRST_RECORD = 1
# A session's fields a reply gives
SESSION_FIELDS = ("id", "title", "workspace", "state", "mode", "archived",
                  "updated_at")


class SearchError(ValueError):
    """Why no search was made, in words ZERO and a person both read."""


def find(index, sessions, logs, query=None, session=None, around=None,
         count=None, asking=None):
    """One search. `sessions`: the session list's rows; `logs`: the folder
    of their logs; `asking`: the session that searches, marked in the reply
    (None for a client)."""
    checked(query, session, around, count)
    listed = {row["id"]: shown(row, asking) for row in sessions}
    target = one_session(listed, session) if session is not None else None
    if around is not None and target is None:
        raise SearchError("around needs session: the session the record is "
                          "in.")
    if target is not None and (around is not None or query is None):
        # A session named alone is read from its start
        around = FIRST_RECORD if around is None else around
        each_side = EACH_SIDE if count is None else count
        return near(target, logs / f"{target['id']}.jsonl", around, each_side)
    count = COUNT if count is None else count
    words = fts_query(query)
    if words is None:
        raise SearchError("query has no word to find." if query else
                          "query says what to find; session reads a "
                          "session, near a record with around.")
    with index.lock:
        index.catch_up({sid: logs / f"{sid}.jsonl" for sid in listed})
        reply = matched(index, listed, target, words, count, asking)
        reply["matched_by"] = "every word"
        either = fts_query(query, any_word=True)
        if reply["record_count"] == 0 and either != words:
            reply = matched(index, listed, target, either, count, asking)
            reply["matched_by"] = "any word"
    reply["query"] = query
    marked(reply, logs)
    return reply


def off_branch(path):
    """The seqs of a log's records off its current branch: the parts a
    rewind left behind."""
    records = [r for r in log_records(path) if "seq" in r]
    on = {record["seq"] for record in history.branch(records)} if records else set()
    return {record["seq"] for record in records} - on


def marked(reply, logs):
    """Each record found on a part a rewind left behind marked so."""
    if "records" in reply:
        pairs = [(reply["session"]["id"], record) for record in reply["records"]]
    else:
        pairs = [(row["id"], row["best"]) for row in reply.get("sessions", [])
                 if row.get("best")]
    left = {}
    for sid, record in pairs:
        if sid not in left:
            left[sid] = off_branch(logs / f"{sid}.jsonl")
        if record.get("seq") in left[sid]:
            record["left_behind"] = True


def matched(index, listed, target, words, count, asking):
    """The records an FTS5 query matches: in one session, or by session."""
    if target is not None:
        total, hits = index.session_hits(words, target["id"], count)
        return {"session": target, "record_count": total, "records": hits}
    return matching_sessions(index, listed, words, count, asking)


def checked(query, session, around, count):
    for name, value, kind in (("query", query, str), ("session", session, str)):
        if value is not None and not isinstance(value, kind):
            raise SearchError(f"{name} is a string.")
    for name, value in (("around", around), ("count", count)):
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            raise SearchError(f"{name} is a whole number.")
    if count is not None and count < 1:
        raise SearchError("count is 1 or more.")


def shown(row, asking):
    """A session as a reply gives it."""
    found = {name: row[name] for name in SESSION_FIELDS}
    found["archived"] = bool(found["archived"])
    if asking is not None:
        found["this_session"] = row["id"] == asking
    return found


def one_session(listed, session):
    """A session by its id or the start of it."""
    if session in listed:
        return listed[session]
    matches = [sid for sid in listed if sid.startswith(session)]
    if len(matches) == 1:
        return listed[matches[0]]
    if not matches:
        raise SearchError(f"No session's id starts with {session!r}.")
    raise SearchError(f"{len(matches)} sessions' ids start with {session!r}; "
                      "more of the id names one.")


def matching_sessions(index, listed, words, count, asking=None):
    """The sessions whose records match, best first, the one that asks
    after the others: the first `count`."""
    found = [(sid, n, rowid) for sid, n, rowid in index.sessions_matching(words)
             if sid in listed]
    found.sort(key=lambda item: item[0] == asking)
    first = found[:count]
    hits = index.hits(words, [rowid for _, _, rowid in first])
    rows = [{**listed[sid], "record_count": n, "best": hits.get(rowid)}
            for sid, n, rowid in first]
    return {"session_count": len(found),
            "record_count": sum(n for _, n, _ in found), "sessions": rows}


def log_records(path):
    """A log's whole records; a broken line is left out."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    records = []
    for line in data[: data.rfind(b"\n") + 1].split(b"\n")[:-1]:
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def near(target, path, around, each_side=EACH_SIDE):
    """The searched records nearest record `around`: `each_side` before it,
    the one at it (or the first after it), `each_side` after; each on a
    part a rewind left behind marked so."""
    records = []
    left = off_branch(path)
    for record in log_records(path):
        found = searchable(record)
        if found is None or not found[2]:
            continue
        kind, tool_or_source, text = found
        records.append({"seq": record.get("seq"), "kind": kind,
                        "time": record.get("time"), "text": text,
                        **named(kind, tool_or_source),
                        **({"left_behind": True}
                           if record.get("seq") in left else {})})
    at = next((i for i, r in enumerate(records) if r["seq"] >= around),
              len(records) - 1)
    start = max(0, at - each_side)
    end = at + each_side + 1
    return {"session": target, "around": around, "records": records[start:end],
            "earlier_count": start, "later_count": max(0, len(records) - end)}
