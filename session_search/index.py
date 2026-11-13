"""The index of every session's log: SQLite's full-text search (FTS5), in a
file of its own beside the server's database.

The logs stay the one truth. The index is made from them and can always be
made again, so one that is missing, broken or of another layout is. It is
caught up at each search: each log is read on from where it was last read,
up to its last whole line, so a running session's newest records are found
and a line still being written waits for the next search. A log written anew
(an upgrade replaces the file) is read again from its start; a session no
longer listed (deleted) leaves the index. Only the logs the session list
names are read: an upgrade's backup beside a log is no session.

What a record holds for the search (`searchable`): the user's words, a
notice's text, ZERO's replies (not its thinking), each call with its result
as ZERO saw it, and a compaction's handoff. The system message, notes and
steps aren't searched.

Words match by FTS5's unicode61 tokenizer: a word whole, case and accents
aside. Prior art: Hermes' and OpenClaw's session search, and
deepseek-harness's, all FTS5 with unicode61.
"""

import json
import re
import sqlite3
import threading
from pathlib import Path

from core import history

# The index's layout: an index of another is made again
LAYOUT_VERSION = 1
# The search's own tool: its calls' results repeat records found elsewhere,
# and a later search would find its own earlier ones first
SEARCH_TOOL = "session_search"
SCHEMA = """
CREATE TABLE logs (
    session TEXT PRIMARY KEY,
    -- The log file read, its device and inode: an upgrade writes a new one
    file_id TEXT NOT NULL,
    -- How much of it is read, its whole lines up to there
    bytes_read INTEGER NOT NULL
);
CREATE VIRTUAL TABLE records USING fts5(
    text,
    session UNINDEXED,
    seq UNINDEXED,
    -- user, notice, reply, call or handoff (KINDS)
    kind UNINDEXED,
    -- A call's tool, a notice's source; empty for the rest
    tool_or_source UNINDEXED,
    time UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""
KINDS = ("user", "notice", "reply", "call", "handoff")
# How a match is marked in a snippet (not **, which a reply's Markdown
# holds); its words are the search's (session_search_snippet_words; FTS5
# allows 64)
MARKS = ("«", "»")
# How a record ranks: FTS5's bm25, a call (its command and output) weighed at
# half, so the user's words and ZERO's replies come before the output they
# talk about, which a command's output, saying a word again and again, can
# otherwise bury. Measured on real searches in ZERO's logs: one of seven had
# its answer sixth, behind five commands; first at half; the rest unchanged
CALL_WEIGHT = 0.5
RANK = f"bm25(records) * (CASE kind WHEN 'call' THEN {CALL_WEIGHT} ELSE 1.0 END)"
# The most matches a search's first look ranks (7.5.2: a common word across
# thousands of sessions); the rest are counted by session, not ranked
FIRST_LOOK_RECORDS = 5000
# A query's pieces: "a phrase" (maybe a prefix) or a run of anything else
QUERY_PIECE = re.compile(r'"[^"]*"\*?|[^\s"]+')
OPERATORS = ("OR", "AND", "NOT")


class Unreadable(ValueError):
    """A query FTS5 can't read, with FTS5's own words."""


def searchable(record):
    """What a log record holds for the search: (kind, its tool or source,
    text), or None for a record with nothing searched."""
    kind = record.get("type")
    if kind == "user":
        if history.is_notice(record):
            return "notice", record.get("source", ""), record.get("text", "")
        return "user", "", record.get("text", "")
    if kind == "answer" and record.get("text"):
        # The user's own words, answering an ask
        return "user", "", record["text"]
    if kind == "assistant":
        content = (record.get("message") or {}).get("content")
        if isinstance(content, str) and content.strip():
            return "reply", "", content
        return None
    if kind == "tool":
        tool = record.get("tool", "")
        if tool == SEARCH_TOOL:
            return None
        text = f"{tool} {arguments_text(record.get('arguments'))}\n"
        return "call", tool, text + str(record.get("result", ""))
    if kind == "compaction" and record.get("summary"):
        return "handoff", "", record["summary"]
    return None


def arguments_text(arguments):
    """A call's arguments as their values' text (a command, a path, a
    file's content), not JSON with its escapes; as given when not JSON."""
    try:
        values = json.loads(arguments or "{}")
    except (TypeError, json.JSONDecodeError):
        return str(arguments or "")
    if not isinstance(values, dict):
        return str(arguments)
    return "\n".join(str(value) for value in values.values())


def fts_query(text, any_word=False):
    """Words as FTS5 reads them, or None for no word. Each bare word is
    quoted, so a symbol in it can't break FTS5's grammar; "a phrase" stays
    one, a piece ending in * matches a word's start, and OR, AND and NOT
    (in capitals) are FTS5's own. `any_word`: every word joined by OR."""
    pieces = []
    for piece in QUERY_PIECE.findall(text or ""):
        if piece in OPERATORS:
            if not any_word:
                pieces.append(piece)
            continue
        prefix = piece.endswith("*")
        words = piece[:-1] if prefix else piece
        if len(words) >= 2 and words.startswith('"') and words.endswith('"'):
            words = words[1:-1]
        if not words.strip():
            continue
        quoted = '"' + words.replace('"', '""') + '"'
        pieces.append(quoted + ("*" if prefix else ""))
    if any_word:
        pieces = [p for i, p in enumerate(pieces) if p not in pieces[:i]]
        return " OR ".join(pieces) or None
    # An operator needs a word on each side
    tidy = []
    for piece in pieces:
        if piece in OPERATORS and (not tidy or tidy[-1] in OPERATORS):
            continue
        tidy.append(piece)
    while tidy and tidy[-1] in OPERATORS:
        tidy.pop()
    return " ".join(tidy) or None


def file_id(stat):
    return f"{stat.st_dev}:{stat.st_ino}"


class Index:
    """The index at `path`, made on first use. One server holds it; a lock
    keeps its threads (each session's, the server's) one at a time."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.db = None

    # ------------------------------------------------------------- opening

    def open(self):
        if self.db is None:
            try:
                self.db = self._connect()
            except sqlite3.DatabaseError:
                # Broken: made again, the logs being the truth
                self._remove()
                self.db = self._connect()
        return self.db

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, check_same_thread=False,
                             isolation_level=None)
        db.row_factory = sqlite3.Row
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version != LAYOUT_VERSION:
            tables = db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            if tables:
                # Another layout: made again from the logs
                db.close()
                self._remove()
                db = sqlite3.connect(self.path, check_same_thread=False,
                                     isolation_level=None)
                db.row_factory = sqlite3.Row
            db.executescript(SCHEMA)
            db.execute(f"PRAGMA user_version = {LAYOUT_VERSION}")
        return db

    def _remove(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)

    def close(self):
        with self.lock:
            if self.db is not None:
                self.db.close()
                self.db = None

    # ---------------------------------------------------------- catching up

    def catch_up(self, logs):
        """Read on in each log; `logs`: {session: its log's path}, every
        session the index holds from now on."""
        db = self.open()
        known = {row["session"]: row for row in db.execute("SELECT * FROM logs")}
        db.execute("BEGIN")
        try:
            for sid in set(known) - set(logs):
                self._drop(db, sid)
            for sid, path in logs.items():
                self._read_on(db, sid, Path(path), known.get(sid))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def _drop(self, db, sid):
        db.execute("DELETE FROM records WHERE session = ?", (sid,))
        db.execute("DELETE FROM logs WHERE session = ?", (sid,))

    def _read_on(self, db, sid, path, row):
        try:
            stat = path.stat()
        except FileNotFoundError:
            # Named but not written yet (a new session), or gone
            if row is not None:
                self._drop(db, sid)
            return
        start = 0
        if row is not None:
            same = row["file_id"] == file_id(stat)
            if same and row["bytes_read"] <= stat.st_size:
                start = row["bytes_read"]
            else:
                self._drop(db, sid)
        if row is not None and start == stat.st_size:
            return
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(stat.st_size - start)
        whole = data[: data.rfind(b"\n") + 1]
        # A fork's log opens with the records it copied from the session it
        # came from, all written at once: found there, not again here
        copied_to = 0
        for line in whole.split(b"\n")[:-1]:
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                # A broken line holds nothing to find; the log's reader says
                # what is wrong with it
                continue
            if not isinstance(record, dict):
                continue
            if start == 0 and record.get("type") == "session":
                copied_to = (record.get("forked_from") or {}).get("seq", 0)
            if copied_to and (record.get("seq") or 0) <= copied_to:
                continue
            found = searchable(record)
            if found is None or not found[2]:
                continue
            kind, tool_or_source, text = found
            db.execute(
                "INSERT INTO records (text, session, seq, kind, tool_or_source,"
                " time) VALUES (?, ?, ?, ?, ?, ?)",
                (text, sid, record.get("seq"), kind, tool_or_source,
                 record.get("time")),
            )
        db.execute(
            "INSERT OR REPLACE INTO logs (session, file_id, bytes_read) "
            "VALUES (?, ?, ?)",
            (sid, file_id(stat), start + len(whole)),
        )

    # ------------------------------------------------------------ searching

    def _match(self, sql, query, *values):
        try:
            return self.open().execute(sql, (query, *values)).fetchall()
        except sqlite3.OperationalError as error:
            raise Unreadable(str(error)) from None

    def sessions_matching(self, query):
        """[(session, its matching records' count, its best record's rowid)],
        best first, for an FTS5 query. Only the best FIRST_LOOK_RECORDS
        matches are ranked; a session whose matches all rank below them
        comes last, its count kept and its best rowid None."""
        rows = self._match(
            "SELECT rowid, session FROM records WHERE records MATCH ? "
            f"ORDER BY {RANK} LIMIT ?", query, FIRST_LOOK_RECORDS,
        )
        counts = {row["session"]: row["n"] for row in self._match(
            "SELECT session, count(*) AS n FROM records WHERE records MATCH ? "
            "GROUP BY session", query,
        )}
        best = {}
        for row in rows:
            best.setdefault(row["session"], row["rowid"])
        ranked = [(sid, counts.get(sid, 1), rowid) for sid, rowid in best.items()]
        return ranked + [(sid, n, None) for sid, n in counts.items()
                         if sid not in best]

    def hits(self, query, rowids, snippet_words):
        """The records with these rowids, each with its snippet
        (`snippet_words` long at most), by rowid."""
        if not rowids:
            return {}
        marks = ", ".join("?" * len(rowids))
        rows = self._match(
            f"SELECT rowid, {self._fields(snippet_words)} FROM records "
            f"WHERE records MATCH ? AND rowid IN ({marks})", query, *rowids,
        )
        return {row["rowid"]: hit(row) for row in rows}

    def session_hits(self, query, sid, count, snippet_words):
        """(how many of a session's records match, the best `count` of them,
        each with its snippet, `snippet_words` long at most)."""
        total = len(self._match(
            "SELECT rowid FROM records WHERE records MATCH ? AND session = ?",
            query, sid,
        ))
        rows = self._match(
            f"SELECT rowid, {self._fields(snippet_words)} FROM records "
            f"WHERE records MATCH ? AND session = ? ORDER BY {RANK} "
            "LIMIT ?", query, sid, count,
        )
        return total, [hit(row) for row in rows]

    @staticmethod
    def _fields(snippet_words):
        # A whole number from the settings, never the caller's text
        return (
            "session, seq, kind, tool_or_source, time, "
            f"snippet(records, 0, '{MARKS[0]}', '{MARKS[1]}', '…', "
            f"{int(snippet_words)}) "
            "AS snippet"
        )


def hit(row):
    """A matching record as a search's reply gives it."""
    found = {"seq": row["seq"], "kind": row["kind"], "time": row["time"],
             "snippet": row["snippet"]}
    found.update(named(row["kind"], row["tool_or_source"]))
    return found


def named(kind, tool_or_source):
    """A call's tool or a notice's source, under its own name."""
    if kind == "call":
        return {"tool": tool_or_source}
    if kind == "notice":
        return {"source": tool_or_source}
    return {}
