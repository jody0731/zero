"""The runtime's state in SQLite: what is waiting and what is running.

History lives in the session logs (sessions/log.py), the truth (design rule
9); this holds the inbox, the session list and the asks waiting for a reply,
and the server services' tables (jobs/store.py). What of it the log says is
rebuilt from the log (server/rebuild.py): a session's row keeps the newest
record its rows are in step with (`log_seq`). The database is born with its
whole schema; a column renamed since a database was made is renamed in place
when it opens (`RENAMED_COLUMNS`).

One writer, many readers (7.5.2: the database off its one connection and
lock). Every change is a function of the write connection, handed to the
database's writer thread through a capped queue (`write`): the writer takes
what is waiting, runs each change in a savepoint of its own (one that fails
is undone alone and its caller gets the error), and commits them in one
transaction, one flush to the disk for them all (a group commit, as Codex's
log database batches its queue). Each caller waits for that commit, so a
message that got its ack is on disk (synchronous FULL). Reads take a
connection from a pool and never wait on a write: SQLite's WAL lets them read
the last commit while the writer writes. `on_change` hears each committed
transaction, on the writer's thread: the server's live session list.
"""

import json
import os
import queue
import sqlite3
import sys
import threading
import time
import traceback
import weakref
from datetime import datetime

from core import history

# The database's format, its user_version: a newer ZERO's database is read for
# what this one knows of it, and said
FILE_FORMAT = 2
# The most changes one transaction takes; the rest wait for the next one
BATCH_MOST = 256
# Changes waiting for the writer at most (design rule 12: every queue has a
# cap); a caller past it waits for room
QUEUE_MOST = 10_000
# Read connections at most, taken in turn from a pool: one a core, within
# reason
READERS_MOST = min(16, max(4, os.cpu_count() or 4))
# How long a connection waits for SQLite's own lock (another process's
# checkpoint, a tool reading the file) before it fails
BUSY_SECONDS = 10

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
    started_by TEXT,
    -- The newest log record its rows are in step with: a log past it is
    -- rebuilt from at start (server/rebuild.py)
    log_seq INTEGER NOT NULL DEFAULT 0
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
    -- The rule's words with the reason, as the models read them
    why TEXT NOT NULL,
    -- Its parts: the reason the user reads ('' for none), and what the ask
    -- is about where the rule says it
    reason TEXT NOT NULL DEFAULT '',
    about TEXT,
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
ADDED_COLUMNS = {
    "sessions": {"parent": "TEXT", "started_by": "TEXT",
                 "log_seq": "INTEGER NOT NULL DEFAULT 0"},
    "asks": {"reason": "TEXT NOT NULL DEFAULT ''", "about": "TEXT"},
}
# Beside the database, a copy of it as it was before a rename
BACKUP_SUFFIX = ".before-renames-"
# A user message that waits for the turn's answer instead of steering it
FOLLOW_UP = "json_extract(payload, '$.follow_up') IS NOT NULL"
# A notice: any message but a user's and a stop (a job's, jobs/service.py)
NOTICE = "kind NOT IN ('user', 'stop')"
# The stops not yet done sent to a turn: (session, the turn's message's
# arrival and its id twice); a stop names its turn, or else is the next one's
STOP_OF_TURN = (
    "session = ? AND kind = 'stop' AND status != 'done' AND arrival > ? AND "
    "COALESCE(json_extract(payload, '$.turn'), ?) = ?"
)


def stops_done(db, session, arrival):
    """Inside a transaction: the stops sent to the turn of the message that
    arrived at `arrival` are done with it."""
    row = db.execute("SELECT id FROM inbox WHERE arrival = ?", (arrival,)).fetchone()
    if row is not None:
        db.execute(f"UPDATE inbox SET status = 'done' WHERE {STOP_OF_TURN}",
                   (session, arrival, row[0], row[0]))


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


class Write:
    """One change waiting for the database's writer: a function of the write
    connection, then its result or its error once the commit is done."""

    __slots__ = ("change", "done", "result", "error")

    def __init__(self, change):
        self.change = change
        self.done = threading.Event()
        self.result = None
        self.error = None

    def answer(self):
        """Its result, once committed; its error raised in the caller."""
        self.done.wait()
        if self.error is not None:
            raise self.error
        return self.result


# What tells the writer to stop, in its queue after what is waiting
STOP = object()


class Writer:
    """The database's one write connection and the thread that owns it: it
    takes the changes waiting in its queue, up to BATCH_MOST, and commits
    them in one transaction, each in a savepoint of its own."""

    def __init__(self, db, changed):
        """`changed()`: told after each commit, on this thread."""
        self.db = db
        self.changed = changed
        self.queue = queue.Queue(QUEUE_MOST)
        self.thread = threading.Thread(target=self.run, name="database-writer",
                                       daemon=True)
        self.thread.start()

    def put(self, write):
        self.queue.put(write)

    def on_its_thread(self):
        return threading.current_thread() is self.thread

    def run(self):
        while True:
            first = self.queue.get()
            if first is STOP:
                break
            batch, stop = [first], False
            while len(batch) < BATCH_MOST:
                try:
                    write = self.queue.get_nowait()
                except queue.Empty:
                    break
                if write is STOP:
                    stop = True
                    break
                batch.append(write)
            self.commit(batch)
            if stop:
                break
        self.db.close()

    def take_batch(self, batch):
        """Run each change in its savepoint, in one transaction left open:
        True if it may be committed, False once SQLite gave the transaction
        up (each write then has its error)."""
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        for write in batch:
            db.execute("SAVEPOINT one_write")
            try:
                write.result = write.change(db)
            except Exception as error:
                write.error = error
                if not db.in_transaction:
                    # SQLite undid the whole transaction (a full disk):
                    # nothing of the batch stays
                    for other in batch:
                        other.error = other.error or error
                        other.result = None
                    return False
                db.execute("ROLLBACK TO one_write")
            db.execute("RELEASE one_write")
        return True

    def commit(self, batch):
        db = self.db
        try:
            if self.take_batch(batch):
                db.execute("COMMIT")
        except Exception as error:
            if db.in_transaction:
                db.execute("ROLLBACK")
            for write in batch:
                write.error = write.error or error
                write.result = None
        if any(write.error is None for write in batch):
            # Told before the callers go on, as a change's caller would
            try:
                self.changed()
            except Exception:
                # The change is committed; a listener's failure is the
                # listener's, not the callers'
                traceback.print_exc()
        for write in batch:
            write.done.set()

    def stop(self):
        if self.thread.is_alive():
            self.queue.put(STOP)
            if not self.on_its_thread():
                self.thread.join()


def connect(path):
    db = sqlite3.connect(path, check_same_thread=False, isolation_level=None,
                         timeout=BUSY_SECONDS)
    db.row_factory = sqlite3.Row
    return db


class Store:
    def __init__(self, path, schemas=(), renamed_columns=(), added_columns=()):
        """`schemas`: the server services' tables, made with the kernel's;
        `renamed_columns` and `added_columns`: their RENAMED_COLUMNS and
        ADDED_COLUMNS, renamed and added with the kernel's."""
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        db = connect(path)
        db.execute("PRAGMA journal_mode = WAL")
        # Each commit reaches the disk: an acked message survives a power cut
        db.execute("PRAGMA synchronous = FULL")
        db.execute("PRAGMA foreign_keys = ON")
        rename_columns(db, path, {
            **RENAMED_COLUMNS,
            **{table: renames for each in renamed_columns
               for table, renames in each.items()},
        })
        db.executescript(SCHEMA)
        for schema in schemas:
            db.executescript(schema)
        add_columns(db, {
            **ADDED_COLUMNS,
            **{table: columns for each in added_columns
               for table, columns in each.items()},
        })
        found = db.execute("PRAGMA user_version").fetchone()[0]
        if found > FILE_FORMAT:
            print(f"ZERO's server: {path} was written by a newer ZERO (its "
                  f"format {found}; this one knows {FILE_FORMAT}): what "
                  "this ZERO knows of it is read", file=sys.stderr, flush=True)
        else:
            db.execute(f"PRAGMA user_version = {FILE_FORMAT}")
        self.on_change = None
        # The writer reaches the store weakly, so a store let go of stops
        # its thread
        mine = weakref.ref(self)

        def changed():
            store = mine()
            if store is not None and store.on_change is not None:
                store.on_change()

        self.writer = Writer(db, changed)
        # The read connections, opened as they are first needed, up to
        # READERS_MOST; a reader waits for one only when all are in use
        self.readers = queue.LifoQueue()
        self.readers_opened = 0
        self.readers_lock = threading.Lock()
        weakref.finalize(self, close_all, self.writer, self.readers)

    @property
    def db(self):
        """The write connection, for a look from the writer's own thread
        (and tools that read a store at rest)."""
        return self.writer.db

    def write(self, change):
        """`change(db)` run by the database's writer inside a transaction,
        in a savepoint of its own; its result once committed, or its error
        raised here (the change undone). From inside a change, it runs at
        once, in the same transaction."""
        if self.writer.on_its_thread():
            return change(self.writer.db)
        write = Write(change)
        self.writer.put(write)
        return write.answer()

    def reader(self):
        try:
            return self.readers.get_nowait()
        except queue.Empty:
            pass
        with self.readers_lock:
            room = self.readers_opened < READERS_MOST
            if room:
                self.readers_opened += 1
        if not room:
            return self.readers.get()
        db = connect(self.path)
        # Only reads go through it; a write here is a bug, said at once
        db.execute("PRAGMA query_only = ON")
        return db

    def query(self, sql, *args):
        if self.writer.on_its_thread():
            # Inside a change: what the transaction sees so far
            return [dict(row) for row in self.writer.db.execute(sql, args)]
        db = self.reader()
        try:
            return [dict(row) for row in db.execute(sql, args)]
        finally:
            self.readers.put(db)

    def close(self):
        """Commit what waits, stop the writer, close every connection."""
        close_all(self.writer, self.readers)

    def admit(self, msg_id, session, kind, source, payload, new_session=None):
        """Put a message in the inbox. Returns (admitted, session id).

        `new_session`: the row of a session this message starts (title, source,
        model, mode, workspace, and a switch its first turn takes), created in
        the same transaction; a session id already taken raises
        sqlite3.IntegrityError, and nothing is written. A message whose id is
        already in the inbox isn't admitted again; its session is returned.
        """
        now = time.time()

        def change(db):
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

        return self.write(change)

    def begin(self, arrival, session):
        """A turn started on this message: the row delivered, the session running."""
        self._move(arrival, session, "delivered", "running")

    def finish(self, arrival, session, state, log_seq=None):
        """Its turn ended: the row done, and the stops sent to it; the session
        idle or failed, its rows in step with the log up to `log_seq` (its
        turn_end's), when given."""
        self._move(arrival, session, "done", state, log_seq)

    def requeue(self, arrival, session):
        self._move(arrival, session, "queued", "idle")

    def resume(self, arrival, session, counted=True):
        """Count the try and queue it atomically, even if boot then cuts off.
        `counted` False: a clean stop's turn, whose mark the resume clears."""

        def change(db):
            db.execute(
                "UPDATE inbox SET resume_count = resume_count + ?, clean_stop = 0, "
                "status = 'queued' WHERE arrival = ?", (int(counted), arrival),
            )
            db.execute(
                "UPDATE sessions SET state = 'idle', updated_at = ? WHERE id = ?",
                (time.time(), session),
            )

        self.write(change)

    def _move(self, arrival, session, status, state, log_seq=None):
        def change(db):
            db.execute(
                "UPDATE inbox SET status = ? WHERE arrival = ?", (status, arrival)
            )
            if status == "done":
                stops_done(db, session, arrival)
            db.execute(
                "UPDATE sessions SET state = ?, updated_at = ?, "
                "log_seq = COALESCE(?, log_seq) WHERE id = ?",
                (state, time.time(), log_seq, session),
            )

        self.write(change)

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

    def has_queued(self, session):
        """Whether the session has a message that can start a turn."""
        return bool(self.query(
            f"SELECT 1 FROM inbox WHERE session = ? AND {self.STARTS} LIMIT 1",
            session,
        ))

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

        def change(db):
            rows = db.execute(
                "SELECT * FROM inbox WHERE session = ? AND status = 'queued' AND "
                "kind = 'user' AND arrival > ? ORDER BY arrival", (session, after),
            ).fetchall()
            db.executemany(
                "UPDATE inbox SET status = 'done' WHERE arrival = ?",
                [(row["arrival"],) for row in rows],
            )
            return [dict(row) for row in rows]

        result = []
        for row in self.write(change):
            payload = json.loads(row["payload"])
            result.append({
                "id": row["id"], "text": payload["text"], "source": row["source"],
                "follow_up": bool(payload.get("follow_up")),
            })
        return result

    def mark_steer_done(self, msg_id):
        """A steer is in the log; don't release the turn's claim."""
        self.write(lambda db: db.execute(
            "UPDATE inbox SET status = 'done' WHERE id = ?", (msg_id,)))

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
        waiting, its rows in step with the log up to the ask. Kept once: the
        boot repair may write it again."""
        created_at = datetime.fromisoformat(ask["time"]).timestamp()

        def change(db):
            db.execute(
                "INSERT OR IGNORE INTO asks (id, session, call, turn, tool, command, "
                "why, reason, about, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (ask["ask"], session, ask["call"], turn, ask["tool"],
                 str(ask["command"]), ask["why"], ask["reason"],
                 ask.get("about"), created_at),
            )
            db.execute(
                "UPDATE sessions SET state = 'waiting', updated_at = ?, "
                "log_seq = ? WHERE id = ?", (time.time(), ask["seq"], session),
            )

        self.write(change)

    def close_ask(self, ask, reply, yes, source):
        """Its reply is in the log: the ask closed, the reply's row done, the
        session running again."""
        now = time.time()

        def change(db):
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

        self.write(change)

    def mark_clean_stop(self, msg_id):
        self.write(lambda db: db.execute(
            "UPDATE inbox SET clean_stop = 1 WHERE id = ?", (msg_id,)))

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
        self.write(lambda db: db.execute(
            "UPDATE sessions SET state = ?, updated_at = ? WHERE id = ?",
            (state, time.time(), session)))

    def queue_notice(self, db, msg_id, session, source, payload):
        """Queue a notice inside a service's own change (jobs/store.py), so
        its row and its notice commit together; kept once by its id."""
        db.execute(
            "INSERT OR IGNORE INTO inbox (id, session, kind, source, payload, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, 'queued', ?)",
            (msg_id, session, source, source, json.dumps(payload), time.time()),
        )

    def withdraw(self, msg_id):
        """A notice the model no longer needs: done, if no turn took it yet."""
        self.write(lambda db: db.execute(
            "UPDATE inbox SET status = 'done' WHERE id = ? AND status = 'queued'",
            (msg_id,)))

    def rebuild(self, session, rows, records, services=()):
        """The session's rows as its log says (server/rebuild.py's
        KernelRows), and each service's (`services`: their `rebuild`), in one
        transaction."""

        def change(db):
            db.execute(
                "UPDATE sessions SET state = ?, end_seq = ?, log_seq = ? "
                "WHERE id = ?", (rows.state, rows.end_seq, rows.log_seq, session),
            )
            db.execute("DELETE FROM asks WHERE session = ?", (session,))
            db.executemany(
                "INSERT INTO asks (id, session, call, turn, tool, command, why, "
                "reason, about, status, created_at, closed_at, reply, source) "
                "VALUES (:id, :session, :call, :turn, :tool, :command, :why, "
                ":reason, :about, :status, :created_at, :closed_at, :reply, "
                ":source)",
                [{**ask, "session": session} for ask in rows.asks],
            )
            for id, status in rows.inbox.items():
                db.execute(
                    "UPDATE inbox SET status = ? WHERE id = ? AND session = ?",
                    (status, id, session),
                )
                found = db.execute(
                    "SELECT arrival FROM inbox WHERE id = ?", (id,)
                ).fetchone()
                if status == "done" and found is not None:
                    stops_done(db, session, found[0])
            for rebuild in services:
                rebuild(db, session, records)

        self.write(change)

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

    def session_ids(self):
        """Every session's id."""
        return {row["id"] for row in self.query("SELECT id FROM sessions")}

    def sessions(self):
        """Every session, the most recently active first."""
        return self.query(
            "SELECT * FROM sessions ORDER BY (state = 'waiting') DESC, "
            "updated_at DESC, created_at DESC"
        )

    # What the user has seen, and housekeeping

    def mark_ended(self, session, seq):
        """A turn ended at this record: news until the user sees it."""
        self.write(lambda db: db.execute(
            "UPDATE sessions SET end_seq = MAX(end_seq, ?) WHERE id = ?",
            (seq, session)))

    def mark_seen(self, session, seq):
        """The user has seen the session up to `seq`, and worked in it now."""
        self.write(lambda db: db.execute(
            "UPDATE sessions SET seen_seq = MAX(seen_seq, ?), seen_at = ? "
            "WHERE id = ?", (seq, time.time(), session)))

    def last_seen(self):
        """The session the user last worked in, not archived; or None."""
        rows = self.query(
            "SELECT id FROM sessions WHERE seen_at IS NOT NULL AND archived = 0 "
            "ORDER BY seen_at DESC LIMIT 1"
        )
        return rows[0]["id"] if rows else None

    def add_session(self, session, row, rows_beside=None):
        """A session made without a message, idle (a fork, a subagent, a log
        adopted): its title, source, model, mode, workspace, a switch its
        first turn takes, its parent, who started it, the newest record the
        user has seen, and when it was made (now, unless `row` says).
        `rows_beside(db, session)`: its maker's rows of it (a subagent's row
        and task), written in the same transaction, so none is ever there
        without the others. A session id already taken raises
        sqlite3.IntegrityError, and nothing is written."""
        now = time.time()
        made = row.get("created_at") or now

        def change(db):
            db.execute(
                "INSERT INTO sessions (id, title, source, model, mode, workspace, "
                "switch, parent, started_by, seen_seq, state, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'idle', ?, ?)",
                (session, row["title"], row["source"], row["model"], row["mode"],
                 row["workspace"], row.get("switch"), row.get("parent"),
                 row.get("started_by"), row.get("seen_seq", 0), made,
                 max(made, row.get("updated_at") or made)),
            )
            if rows_beside is not None:
                rows_beside(db, session)

        self.write(change)

    def update_session(self, session, **fields):
        names = ", ".join(f"{name} = ?" for name in fields)
        self.write(lambda db: db.execute(
            f"UPDATE sessions SET {names} WHERE id = ?",
            (*fields.values(), session)))

    def forget(self, session, tables=()):
        """Every row of the session, its log already moved to the trash.
        `tables`: the server services' with a session column (Service.tables)."""

        def change(db):
            for table in ("asks", *tables, "inbox"):
                db.execute(f"DELETE FROM {table} WHERE session = ?", (session,))
            db.execute("DELETE FROM sessions WHERE id = ?", (session,))

        self.write(change)


def close_all(writer, readers):
    """The writer stopped once what waits is committed, and every read
    connection closed (a store closed, or let go of)."""
    writer.stop()
    while True:
        try:
            readers.get_nowait().close()
        except queue.Empty:
            return
