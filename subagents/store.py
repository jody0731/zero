"""The subagents' table in the server's one database (server/store.py).

A subagent's row and its task's notice commit in one transaction, so a
crash can't leave a subagent with no task or a task with no row. Its
session's row is the kernel's (`parent`, `started_by` zero).
"""

import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS subagents (
    -- The subagent's own session
    session TEXT PRIMARY KEY REFERENCES sessions(id),
    -- The session that started it
    parent TEXT NOT NULL,
    -- fork (the parent's finished turns) or handoff (only its task)
    start_from TEXT NOT NULL,
    -- Its level below the session the user started: 1, 2, ...
    depth INTEGER NOT NULL,
    -- The parent's record its start_subagent ran in: a report to a part a
    -- rewind left behind says so
    started_from_seq INTEGER,
    -- The estimate it was started with: minutes of working time, dollars
    estimate_minutes REAL NOT NULL,
    estimate_dollars REAL NOT NULL,
    -- When its parent was told its use passed the estimate; NULL until then
    past_estimate_at REAL,
    started_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS subagents_parent ON subagents (parent);
"""


class SubagentStore:
    """The subagents' rows, over the server's store."""

    def __init__(self, store):
        self.store = store

    def add(self, row, notice):
        """A subagent's row and its task's notice: (inbox id, session,
        source, payload), in one transaction."""
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO subagents (session, parent, start_from, depth, "
                "started_from_seq, estimate_minutes, estimate_dollars, "
                "started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (row["session"], row["parent"], row["start_from"], row["depth"],
                 row["started_from_seq"], row["estimate_minutes"],
                 row["estimate_dollars"], time.time()),
            )
            self.store.queue_notice(db, *notice)

    def subagent(self, session):
        rows = self.store.query("SELECT * FROM subagents WHERE session = ?",
                                session)
        return rows[0] if rows else None

    def children(self, session):
        """The subagents a session started, the oldest first."""
        return self.store.query(
            "SELECT * FROM subagents WHERE parent = ? ORDER BY started_at",
            session,
        )

    def mark_past(self, session, at):
        with self.store.transaction() as db:
            db.execute(
                "UPDATE subagents SET past_estimate_at = ? WHERE session = ?",
                (at, session),
            )

    def notice(self, msg_id, session, source, payload):
        """A notice into a session's inbox, kept once by its id."""
        with self.store.transaction() as db:
            self.store.queue_notice(db, msg_id, session, source, payload)
