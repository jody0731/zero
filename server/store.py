"""The runtime's state in SQLite: what is waiting and what is running.

History lives in the session logs (sessions/log.py); this holds the inbox, the
session list and the asks waiting for a reply, and the server services' tables
(jobs/store.py). Every change is one transaction, committed before the caller
goes on, so a message that got its ack is on disk. The database is born with
its whole schema; a column renamed since a database was made is renamed in
place when it opens (`RENAMED_COLUMNS`). `on_change` hears each committed
transaction: the server's live session list.
"""

import json
import sqlite3
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime

from core import history

# The database's format, its user_version: a newer ZERO's database is read for
# what this one knows of it, and said
FILE_FORMAT = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    -- What its next turn runs with; the user may change them
    model TEXT NOT NULL,
    mode TEXT NOT NULL,
    -- The folder its commands run in; / is the whole machine
    workspace TEXT NOT NULL,
    -- idle, running, waiting, failed
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    -- The newest record the user has seen, and when they last looked
    seen_seq INTEGER NOT NULL DEFAULT 0,
    seen_at REAL,
    -- The newest turn_end's seq: news while it is past seen_seq
    end_seq INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    -- What the user set since the last turn started, as JSON: the next new
    -- turn logs it as a switch and clears it
    switch TEXT,
    -- The session a sub-session sits under (a fork, a subagent); NULL for
    -- any other
    parent TEXT,
    -- Who started it: zero (a subagent), else the user (NULL before 0.6.7)
    started_by TEXT
);
CREATE TABLE IF NOT EXISTS inbox (
    -- The order messages arrived in
    arrival INTEGER PRIMARY KEY AUTOINCREMENT,
    -- The sender's id: a message sent twice is kept once
    id TEXT NOT NULL UNIQUE,
    session TEXT NOT NULL REFERENCES sessions(id),
    -- user, stop, or a notice's source (a job ended, checked in or was lost)
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    payload TEXT NOT NULL,
    -- queued, delivered (its turn is running), done
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    resume_count INTEGER NOT NULL DEFAULT 0,
    -- 1 when a clean stop cut its turn off: the resume uses no try
    clean_stop INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS inbox_open ON inbox (status, session, arrival);
CREATE TABLE IF NOT EXISTS asks (
    -- The `ask` record's id in the session log, which is the truth
    id TEXT PRIMARY KEY,
    session TEXT NOT NULL REFERENCES sessions(id),
    call TEXT NOT NULL,
    -- The inbox id of the turn that parked
    turn TEXT NOT NULL,
    tool TEXT NOT NULL,
    command TEXT NOT NULL,
    why TEXT NOT NULL,
    -- open, approved, cancelled
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    closed_at REAL,
    -- The reply's inbox id and source
    reply TEXT,
    source TEXT
);
CREATE INDEX IF NOT EXISTS asks_open ON asks (session, status);
"""
# Columns renamed since a database was made (0.6.2.8.1: a time ends in _at, a
# count says so): table → {old name: new name}, renamed in place on opening
RENAMED_COLUMNS = {
    "sessions": {"created": "created_at", "updated": "updated_at"},
    "inbox": {"created": "created_at", "resumes": "resume_count"},
    "asks": {"created": "created_at", "closed": "closed_at"},
}
# Columns added since a database was made (0.6.6, 0.6.7): table → {name:
# its SQL type}, added when the store opens (add_columns)
ADDED_COLUMNS = {"sessions": {"parent": "TEXT", "started_by": "TEXT"}}
# Beside the database, a copy of it as it was before a rename
BACKUP_SUFFIX = ".before-renames-"
# A user message that waits for the turn's answer instead of steering it
FOLLOW_UP = "json_extract(payload, '$.follow_up') IS NOT NULL"
# A notice: any message but a user's and a stop (a job's, jobs/service.py)
NOTICE = "kind NOT IN ('user', 'stop')"


class UnknownSession(Exception):
    pass


def renames_due(db, renamed_columns):
    """[(table, old, new)] for each old column a table still has, its new name
    not there yet."""
    due = []
    for table, renames in renamed_columns.items():
        have = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        due += [(table, old, new) for old, new in renames.items()
                if old in have and new not in have]
    return due


def rename_columns(db, path, renamed_columns):
    """Rename what is due in one transaction, after a copy of the database
    as it was is kept beside it (`BACKUP_SUFFIX` and the time)."""
    due = renames_due(db, renamed_columns)
    if not due:
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = sqlite3.connect(path.with_name(f"{path.name}{BACKUP_SUFFIX}{stamp}"))
    with backup:
        db.backup(backup)
    backup.close()
    db.execute("BEGIN IMMEDIATE")
    for table, old, new in due:
        db.execute(f'ALTER TABLE {table} RENAME COLUMN "{old}" TO "{new}"')
    db.execute("COMMIT")


def add_columns(db, added_columns):
    """Add each column a table made before it lacks: no copy needed, since
    nothing is lost and an older ZERO reads past it."""
    for table, columns in added_columns.items():
        have = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns.items():
            if name not in have:
                db.execute(f'ALTER TABLE {table} ADD COLUMN "{name}" {kind}')


class Store:
    def __init__(self, path, schemas=(), renamed_columns=(), added_columns=()):
        """`schemas`: the server services' tables, made with the kernel's;
        `renamed_columns` and `added_columns`: their RENAMED_COLUMNS and
        ADDED_COLUMNS, renamed and added with the kernel's."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # One connection shared by the runtime's threads, one transaction at a time
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode = WAL")
            # Each commit reaches the disk: an acked message survives a power cut
            self.db.execute("PRAGMA synchronous = FULL")
            self.db.execute("PRAGMA foreign_keys = ON")
            rename_columns(self.db, path, {
                **RENAMED_COLUMNS,
                **{table: renames for each in renamed_columns
                   for table, renames in each.items()},
            })
            self.db.executescript(SCHEMA)
            for schema in schemas:
                self.db.executescript(schema)
            add_columns(self.db, {
                **ADDED_COLUMNS,
                **{table: columns for each in added_columns
                   for table, columns in each.items()},
            })
            found = self.db.execute("PRAGMA user_version").fetchone()[0]
            if found > FILE_FORMAT:
                print(f"ZERO's server: {path} was written by a newer ZERO (its "
                      f"format {found}; this one knows {FILE_FORMAT}): what "
                      "this ZERO knows of it is read", file=sys.stderr, flush=True)
            else:
                self.db.execute(f"PRAGMA user_version = {FILE_FORMAT}")
        self.on_change = None

    @contextmanager
    def transaction(self):
        """One transaction: committed whole, or rolled back, a failed commit
        included, so the connection is never left inside one."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise
        # Outside the lock: a listener may read the store. The change is
        # committed; a listener's failure is the listener's, not the caller's
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                traceback.print_exc()

    def query(self, sql, *args):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args)]

    def admit(self, msg_id, session, kind, source, payload, new_session=None):
        """Put a message in the inbox. Returns (admitted, session id).

        `new_session`: the row of a session this message starts (title, source,
        model, mode, workspace, and a switch its first turn takes), created in
        the same transaction. A message whose id is
        already in the inbox isn't admitted again; its session is returned.
        """
        now = time.time()
        with self.transaction() as db:
            seen = db.execute(
                "SELECT session FROM inbox WHERE id = ?", (msg_id,)
            ).fetchone()
            if seen:
                return False, seen["session"]
            if new_session:
                db.execute(
                    "INSERT INTO sessions (id, title, source, model, mode, "
                    "workspace, switch, state, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'idle', ?, ?)",
                    (
                        session,
                        new_session["title"],
                        new_session["source"],
                        new_session["model"],
                        new_session["mode"],
                        new_session.get("workspace", "/"),
                        new_session.get("switch"),
                        now,
                        now,
                    ),
                )
            elif not db.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session,)
            ).fetchone():
                raise UnknownSession(session)
            db.execute(
                "INSERT INTO inbox (id, session, kind, source, payload, status, "
                "created_at) VALUES (?, ?, ?, ?, ?, 'queued', ?)",
                (msg_id, session, kind, source, json.dumps(payload), now),
            )
        return True, session

    def begin(self, arrival, session):
        """A turn started on this message: the row delivered, the session running."""
        self._move(arrival, session, "delivered", "running")

    def finish(self, arrival, session, state):
        """Its turn ended: the row done, the session idle or failed."""
        self._move(arrival, session, "done", state)

    def requeue(self, arrival, session):
        self._move(arrival, session, "queued", "idle")

    def resume(self, arrival, session, counted=True):
        """Count the try and queue it atomically, even if boot then cuts off.
        `counted` False: a clean stop's turn, whose mark the resume clears."""
        with self.transaction() as db:
            db.execute(
                "UPDATE inbox SET resume_count = resume_count + ?, clean_stop = 0, "
                "status = 'queued' WHERE arrival = ?", (int(counted), arrival),
            )
            db.execute(
                "UPDATE sessions SET state = 'idle', updated_at = ? WHERE id = ?",
                (time.time(), session),
            )

    def _move(self, arrival, session, status, state):
        with self.transaction() as db:
            db.execute(
                "UPDATE inbox SET status = ? WHERE arrival = ?", (status, arrival)
            )
            db.execute(
                "UPDATE sessions SET state = ?, updated_at = ? WHERE id = ?",
                (state, time.time(), session),
            )

    # A notice starts a turn only while its session isn't parked on an ask (a
    # notice is never the reply)
    STARTS = (
        f"status = 'queued' AND (kind = 'user' OR ({NOTICE} AND "
        "(SELECT state != 'waiting' FROM sessions "
        "WHERE sessions.id = inbox.session)))"
    )

    def next_queued(self, session):
        rows = self.query(
            f"SELECT * FROM inbox WHERE session = ? AND {self.STARTS} "
            "ORDER BY arrival LIMIT 1",
            session,
        )
        return rows[0] if rows else None

    def waiting_sessions(self):
        """Sessions with messages that can start a turn, the longest waiting
        first."""
        rows = self.query(
            f"SELECT session FROM inbox WHERE {self.STARTS} "
            "GROUP BY session ORDER BY MIN(arrival)",
        )
        return [row["session"] for row in rows]

    def steers(self, session, after, notices_only=False):
        """Messages a running turn takes in: a client's sent after the turn began,
        not a follow-up, and every queued notice. `notices_only`: once
        the turn is stopped, its untaken messages go back to their sender."""
        user = "0" if notices_only else f"(kind = 'user' AND NOT {FOLLOW_UP})"
        rows = self.query(
            "SELECT * FROM inbox WHERE session = ? AND status = 'queued' AND "
            f"(({user} AND arrival > ?) OR {NOTICE}) ORDER BY arrival",
            session, after,
        )
        found = []
        for row in rows:
            payload = json.loads(row["payload"])
            steer = {"text": payload["text"], "inbox": row["id"],
                     "source": row["source"]}
            # What it carries beside its text (a notice's sender's record,
            # its summary: jobs/store.py)
            for name in history.MESSAGE_FIELDS:
                if payload.get(name) is not None:
                    steer[name] = payload[name]
            found.append(steer)
        return found

    def withdraw_messages(self, session, after):
        """A stop takes back the user's steers and follow-ups its turn hasn't
        taken (Pi): done here, and returned for their sender's input."""
        with self.transaction() as db:
            rows = db.execute(
                "SELECT * FROM inbox WHERE session = ? AND status = 'queued' AND "
                "kind = 'user' AND arrival > ? ORDER BY arrival", (session, after),
            ).fetchall()
            db.executemany(
                "UPDATE inbox SET status = 'done' WHERE arrival = ?",
                [(row["arrival"],) for row in rows],
            )
        result = []
        for row in rows:
            payload = json.loads(row["payload"])
            result.append({
                "id": row["id"], "text": payload["text"], "source": row["source"],
                "follow_up": bool(payload.get("follow_up")),
            })
        return result

    def mark_steer_done(self, msg_id):
        """A steer is in the log; don't release the turn's claim."""
        with self.transaction() as db:
            db.execute("UPDATE inbox SET status = 'done' WHERE id = ?", (msg_id,))

    def stops(self, session, row):
        rows = self.query(
            "SELECT * FROM inbox WHERE session = ? AND arrival > ? "
            "AND kind = 'stop' AND status != 'done' ORDER BY arrival",
            session, row["arrival"],
        )
        return [r for r in rows if json.loads(r["payload"]).get("turn", row["id"])
                == row["id"]]

    def park(self, session, ask, turn):
        """A turn parked on `ask` (its log record): the ask open, the session
        waiting. Kept once: the boot repair may write it again."""
        created_at = datetime.fromisoformat(ask["time"]).timestamp()
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO asks (id, session, call, turn, tool, command, "
                "why, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (ask["ask"], session, ask["call"], turn, ask["tool"],
                 str(ask["command"]), ask["why"], created_at),
            )
            db.execute(
                "UPDATE sessions SET state = 'waiting', updated_at = ? WHERE id = ?",
                (time.time(), session),
            )

    def close_ask(self, ask, reply, yes, source):
        """Its reply is in the log: the ask closed, the reply's row done, the
        session running again."""
        now = time.time()
        with self.transaction() as db:
            db.execute(
                "UPDATE asks SET status = ?, closed_at = ?, reply = ?, source = ? "
                "WHERE id = ?",
                ("approved" if yes else "cancelled", now, reply, source, ask),
            )
            db.execute("UPDATE inbox SET status = 'done' WHERE id = ?", (reply,))
            db.execute(
                "UPDATE sessions SET state = 'running', updated_at = ? WHERE id = "
                "(SELECT session FROM asks WHERE id = ?)", (now, ask),
            )

    def mark_clean_stop(self, msg_id):
        with self.transaction() as db:
            db.execute("UPDATE inbox SET clean_stop = 1 WHERE id = ?", (msg_id,))

    def open_asks(self):
        return self.query("SELECT * FROM asks WHERE status = 'open'")

    def open_ask(self, session):
        """The session's ask waiting for a reply, or None."""
        rows = self.query(
            "SELECT * FROM asks WHERE session = ? AND status = 'open' "
            "ORDER BY created_at DESC LIMIT 1", session,
        )
        return rows[0] if rows else None

    def set_state(self, session, state):
        with self.transaction() as db:
            db.execute(
                "UPDATE sessions SET state = ?, updated_at = ? WHERE id = ?",
                (state, time.time(), session),
            )

    def queue_notice(self, db, msg_id, session, source, payload):
        """Queue a notice inside a service's own transaction (jobs/store.py), so
        its row and its notice commit together; kept once by its id."""
        db.execute(
            "INSERT OR IGNORE INTO inbox (id, session, kind, source, payload, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, 'queued', ?)",
            (msg_id, session, source, source, json.dumps(payload), time.time()),
        )

    def withdraw(self, msg_id):
        """A notice the model no longer needs: done, if no turn took it yet."""
        with self.transaction() as db:
            db.execute(
                "UPDATE inbox SET status = 'done' WHERE id = ? AND status = 'queued'",
                (msg_id,),
            )

    def unfinished_rows(self):
        """Rows not done, oldest first: what the boot repair looks at."""
        return self.query("SELECT * FROM inbox WHERE status != 'done' ORDER BY arrival")

    def rows(self, session):
        return self.query(
            "SELECT * FROM inbox WHERE session = ? ORDER BY arrival", session
        )

    def session(self, session):
        rows = self.query("SELECT * FROM sessions WHERE id = ?", session)
        return rows[0] if rows else None

    def sessions(self):
        """Every session, the most recently active first."""
        return self.query(
            "SELECT * FROM sessions ORDER BY (state = 'waiting') DESC, "
            "updated_at DESC, created_at DESC"
        )

    # What the user has seen, and housekeeping

    def mark_ended(self, session, seq):
        """A turn ended at this record: news until the user sees it."""
        with self.transaction() as db:
            db.execute(
                "UPDATE sessions SET end_seq = MAX(end_seq, ?) WHERE id = ?",
                (seq, session),
            )

    def mark_seen(self, session, seq):
        """The user has seen the session up to `seq`, and worked in it now."""
        with self.transaction() as db:
            db.execute(
                "UPDATE sessions SET seen_seq = MAX(seen_seq, ?), seen_at = ? "
                "WHERE id = ?", (seq, time.time(), session),
            )

    def last_seen(self):
        """The session the user last worked in, not archived; or None."""
        rows = self.query(
            "SELECT id FROM sessions WHERE seen_at IS NOT NULL AND archived = 0 "
            "ORDER BY seen_at DESC LIMIT 1"
        )
        return rows[0]["id"] if rows else None

    def add_session(self, session, row):
        """A session made without a message, idle (a fork, a subagent): its
        title, source, model, mode, workspace, a switch its first turn takes,
        its parent, who started it, and the newest record the user has
        seen."""
        now = time.time()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO sessions (id, title, source, model, mode, workspace, "
                "switch, parent, started_by, seen_seq, state, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'idle', ?, ?)",
                (session, row["title"], row["source"], row["model"], row["mode"],
                 row["workspace"], row.get("switch"), row.get("parent"),
                 row.get("started_by"), row.get("seen_seq", 0), now, now),
            )

    def update_session(self, session, **fields):
        names = ", ".join(f"{name} = ?" for name in fields)
        with self.transaction() as db:
            db.execute(
                f"UPDATE sessions SET {names} WHERE id = ?",
                (*fields.values(), session),
            )

    def forget(self, session, tables=()):
        """Every row of the session, its log already moved to the trash.
        `tables`: the server services' with a session column (Service.tables)."""
        with self.transaction() as db:
            for table in ("asks", *tables, "inbox"):
                db.execute(f"DELETE FROM {table} WHERE session = ?", (session,))
            db.execute("DELETE FROM sessions WHERE id = ?", (session,))
