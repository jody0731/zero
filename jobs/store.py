"""The jobs' table in the server's one database (server/store.py).

A job's change of state and its notice commit in one transaction, so a crash
can't leave one without the other.
"""

from jobs.tool import SOURCE

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    session TEXT NOT NULL REFERENCES sessions(id),
    command TEXT NOT NULL,
    pid INTEGER NOT NULL,
    -- The pid's start time (/proc), so a pid used again is never taken for it
    pid_start TEXT,
    -- running, ended, stopped, lost
    state TEXT NOT NULL,
    exit_code INTEGER,
    started_at REAL NOT NULL,
    ended_at REAL,
    -- When the model's estimate runs out; NULL with none
    due_at REAL,
    -- When it next checks in, while it runs
    next_checkin_at REAL NOT NULL,
    checkin_count INTEGER NOT NULL DEFAULT 0,
    -- How far into its output the model has looked
    read_bytes INTEGER NOT NULL DEFAULT 0,
    -- model or user
    stopped_by TEXT,
    -- The session log's record the job was started from (the call's), so a
    -- notice from a part a rewind left behind says so
    started_from_seq INTEGER
);
CREATE INDEX IF NOT EXISTS jobs_running ON jobs (state, session);
"""
# Columns renamed since a database was made (0.6.2.8.1), renamed in place when
# the server's store opens (server/store.py)
RENAMED_COLUMNS = {
    "jobs": {"code": "exit_code", "started": "started_at", "ended": "ended_at",
             "due": "due_at", "checkin": "next_checkin_at",
             "checkins": "checkin_count", "read": "read_bytes"},
}
# Columns added since a database was made (0.6.6), added when the server's
# store opens
ADDED_COLUMNS = {"jobs": {"started_from_seq": "INTEGER"}}


class JobStore:
    """The jobs' rows, over the server's store."""

    def __init__(self, store):
        self.store = store

    def add_job(self, id, session, command, pid, started_at, due_at,
                next_checkin_at, pid_start=None, started_from_seq=None):
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO jobs (id, session, command, pid, pid_start, state, "
                "started_at, due_at, next_checkin_at, started_from_seq) "
                "VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)",
                (id, session, command, pid, pid_start, started_at, due_at,
                 next_checkin_at, started_from_seq),
            )

    def job(self, id):
        rows = self.store.query("SELECT * FROM jobs WHERE id = ?", id)
        return rows[0] if rows else None

    def jobs(self, session=None, running=False):
        """Jobs, the oldest first: a session's, or every one; running ones only."""
        sql = "SELECT * FROM jobs WHERE 1"
        args = []
        if session is not None:
            sql += " AND session = ?"
            args.append(session)
        if running:
            sql += " AND state = 'running'"
        return self.store.query(sql + " ORDER BY started_at", *args)

    def update_job(self, id, **fields):
        names = ", ".join(f"{name} = ?" for name in fields)
        with self.store.transaction() as db:
            db.execute(f"UPDATE jobs SET {names} WHERE id = ?", (*fields.values(), id))

    def settle_job(self, id, notice=None, **fields):
        """A running job's change of state and its notice in one transaction, so
        a crash can't leave one without the other. `notice`: (inbox id, text).
        Returns False if the job had already settled."""
        names = ", ".join(f"{name} = ?" for name in fields)
        with self.store.transaction() as db:
            changed = db.execute(
                f"UPDATE jobs SET {names} WHERE id = ? AND state = 'running'",
                (*fields.values(), id),
            ).rowcount
            if not changed:
                return False
            if notice is not None:
                msg_id, text = notice
                row = db.execute(
                    "SELECT session, started_from_seq FROM jobs WHERE id = ?", (id,)
                ).fetchone()
                payload = {"text": text, "job": id}
                if row["started_from_seq"] is not None:
                    # Where it started, for a notice from a part left behind
                    payload["started_from_seq"] = row["started_from_seq"]
                self.store.queue_notice(db, msg_id, row["session"], SOURCE, payload)
        return True

    def withdraw(self, msg_id):
        """A notice the model no longer needs: done, if no turn took it yet."""
        self.store.withdraw(msg_id)
