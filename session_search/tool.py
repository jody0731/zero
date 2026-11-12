"""`session_search`: ZERO's search of every session's log.

The server hands each session its search (service.py), bound to the session
that asks, which the reply marks; without a server there is none, and the
tool isn't offered. It reads ZERO's own records, never the machine, so it
needs no permission, as `read` doesn't; a long answer is fitted to the
context as any result is (context/hooks.py).
"""

from datetime import datetime

from core import history
from core.tools import tool
from session_search.index import Unreadable
from session_search.search import SearchError, limit

DESCRIPTION = (
    "Search what every session's log holds, running or done, this one "
    "included (after the others): the user's messages, notices, your replies "
    "(not your thinking), each call with its result as you saw it (not this "
    "tool's own, whose results repeat other records), and compaction "
    "handoffs. With query alone: the sessions whose records match, best "
    "first, each with how many of its records match and its best one. With "
    "query and session: that session's matching records. With session and "
    "around: the records near one, whole; with session alone, its first "
    "ones. A record matches when it holds every word of the query, each a "
    "whole word, case and accents aside; when no record does, the records "
    "holding any of them come instead, and the reply says so. OR between "
    "words matches any of them, \"quotes\" make a phrase, and a word ending "
    "in * matches the words that start with it. A session's log keeps the "
    "parts of its conversation a rewind left behind, and a record found "
    "there says so; a fork's records copied from the session it came from "
    "are found in that session. It runs outside the sandbox, which hides "
    "the logs from commands."
)
NOT_HERE = "Not searched: session search isn't available in this session."


def kind_text(record):
    """What a record is, in words."""
    kind = record["kind"]
    if kind == "user":
        return "the user's message"
    if kind == "notice":
        source = record.get("source", "")
        return f"a notice from {history.NOTICES.get(source, source)}"
    if kind == "reply":
        return "your reply"
    if kind == "call":
        return f"your {record.get('tool', '')} call and its result"
    return "a compaction's handoff"


def when(time):
    """A record's time, to the minute."""
    return str(time or "")[:16].replace("T", " ")


def state_text(session):
    words = ["this session"] if session.get("this_session") else []
    words.append(session["state"])
    if session["archived"]:
        words.append("archived")
    return ", ".join(words)


def session_text(session, first=False):
    word = "Session" if first else "session"
    return f"{word} {session['id']} \"{session['title']}\""


def record_head(record):
    head = f"#{record['seq']} on {when(record['time'])}, {kind_text(record)}"
    if record.get("left_behind"):
        head += ", on a part a rewind left behind"
    return head


def snippet(record):
    return " ".join(str(record.get("snippet") or "").split())


def counted(count, word):
    """A count with its noun: "1 record", "3 records"."""
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def matched(count):
    return f"{counted(count, 'record')} {'matches' if count == 1 else 'match'}"


def first_of(count, shown):
    return f", the first {shown}" if count > shown else ""


def none_found(reply, where):
    words = "any word" if reply["matched_by"] == "any word" else "every word"
    return f"No record {where} has {words} of: {reply['query']}."


def matched_text(reply, where):
    """What the records shown hold: every word, or, when none does, any."""
    if reply["matched_by"] == "any word":
        return (f"No record {where} has every word of: {reply['query']}; "
                "these have any of them")
    return f"every word of: {reply['query']}"


def found_text(reply):
    """A search's reply as ZERO reads it."""
    if "around" in reply:
        return near_text(reply)
    if "sessions" in reply:
        return sessions_text(reply)
    return records_text(reply)


def sessions_text(reply):
    if not reply["sessions"]:
        return none_found(reply, "in any session")
    count = reply["session_count"]
    have = "has" if count == 1 else "have"
    records = counted(reply["record_count"], "record")
    if reply["matched_by"] == "any word":
        head = (f"{matched_text(reply, 'in any session')}: "
                f"{counted(count, 'session')}, {records} in all.")
    else:
        head = (f"{counted(count, 'session')} {have} records with "
                f"{matched_text(reply, '')}; {records} in all.")
    lines = [f"{head} Best first{first_of(count, len(reply['sessions']))}:"]
    for n, session in enumerate(reply["sessions"], 1):
        updated = when_epoch(session["updated_at"])
        best = session["best"]
        lines += [
            "",
            f"{n}. {session_text(session, first=True)} ({state_text(session)}; "
            f"last active {updated}; workspace {session['workspace']}): "
            f"{matched(session['record_count'])}. Its best, "
            f"{record_head(best)}:",
            f"   {snippet(best)}",
        ]
    return "\n".join(lines)


def records_text(reply):
    session = reply["session"]
    if not reply["records"]:
        return none_found(reply, f"in {session_text(session)}")
    count = reply["record_count"]
    have = "has" if count == 1 else "have"
    where = f"in {session_text(session)}"
    if reply["matched_by"] == "any word":
        head = f"{matched_text(reply, where)}: {counted(count, 'record')}."
    else:
        head = (f"{counted(count, 'record')} {where} {have} "
                f"{matched_text(reply, '')}.")
    lines = [f"{head} Best first{first_of(count, len(reply['records']))}:"]
    for record in reply["records"]:
        lines += ["", f"{record_head(record)}:", f"   {snippet(record)}"]
    return "\n".join(lines)


def near_text(reply):
    session = reply["session"]
    if not reply["records"]:
        return f"{session_text(session, first=True)} has no record to show yet."
    lines = [
        f"{session_text(session, first=True)}, the records near "
        f"#{reply['around']} ({reply['earlier_count']} earlier, "
        f"{reply['later_count']} later):"
    ]
    for record in reply["records"]:
        lines += ["", f"{record_head(record)}:", record["text"]]
    return "\n".join(lines)


def when_epoch(seconds):
    return datetime.fromtimestamp(seconds).astimezone().strftime("%Y-%m-%d %H:%M")


def summary(args):
    words = [f'"{args["query"]}"'] if args.get("query") else []
    for name in ("session", "around", "count"):
        if args.get(name) is not None:
            words.append(f"{name} {args[name]}")
    return "session_search " + " ".join(words)


@tool(
    DESCRIPTION,
    # It reads a log by its record numbers: texts naming records say so
    reads_records=True,
    optional=("query", "session", "around", "count"),
    summary=summary,
    # It reads ZERO's own records and changes nothing
    needs_permission=lambda args: False,
    # A running session's log grows: the same call can find more
    changing=True,
    ready=lambda context: context.get("search") is not None,
    query=("string", "Words to find."),
    session=("string", "A session's id, or its start: the session to search "
             "in, or to read near a record in."),
    around=("integer", "A record's number in that session (a result's #): the "
            "records near it, whole; a number past the last record reads the "
            "last ones."),
    count=("integer", lambda limits, context: (
        "How many sessions, or records, at most, "
        f"{limit(limits, 'session_search_count')} when not given; with no "
        "query, how many on each side, "
        f"{limit(limits, 'session_search_each_side')} when not given."
    )),
)
def session_search(query=None, session=None, around=None, count=None,
                   search=None):
    """`search`: the session's own (service.py), never the model's."""
    if search is None:
        return NOT_HERE
    try:
        reply = search.find(query=query, session=session, around=around,
                            count=count)
    except SearchError as error:
        return f"Not searched: {error}"
    except Unreadable as error:
        return f"Not searched: FTS5 couldn't read the query ({error})."
    return found_text(reply)


# What this component offers the model, in the order it reads them; the
# assembly adds them to core's registry (core/tools.py, add)
TOOLS = (session_search,)
