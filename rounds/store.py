"""The rounds' table in the server's one database (server/store.py).

A run's change and the next round's message commit in one transaction, so a
crash can't leave one without the other.
"""

import time

from rounds.tool import SOURCE

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    id TEXT PRIMARY KEY,
    session TEXT NOT NULL REFERENCES sessions(id),
    objective TEXT NOT NULL,
    -- A command the server runs when ZERO reports done; NULL with none
    done_check TEXT,
    -- running, done, stuck, stopped; waiting only in a database from before
    -- 0.6.7, where a spent budget waited on the user
    state TEXT NOT NULL,
    -- The round under way, or the last one
    round_count INTEGER NOT NULL DEFAULT 1,
    -- ZERO's estimate of the whole: rounds, minutes of its working time,
    -- weighted tokens (NULL in a run from before 0.6.8, estimated in dollars)
    estimate_rounds INTEGER NOT NULL,
    estimate_minutes REAL NOT NULL,
    estimate_weighted_tokens REAL,
    spent_minutes REAL NOT NULL DEFAULT 0,
    spent_weighted_tokens REAL NOT NULL DEFAULT 0,
    -- The user's unit, for the screens' line: the estimate at the session's
    -- price when it was made (0 with no price), and what the calls cost
    estimate_dollars REAL NOT NULL,
    spent_dollars REAL NOT NULL DEFAULT 0,
    -- Rounds in a row that ended failed (a model error, a crash)
    failed_count INTEGER NOT NULL DEFAULT 0,
    -- The inbox id of the message that opened the round under way
    round_inbox TEXT,
    -- going_on, done or stuck: the last end_round's
    last_status TEXT,
    -- The done check's last exit code, and where its output is
    check_exit_code INTEGER,
    check_output_path TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    -- When the work passed its estimate and ZERO was told; NULL until then
    past_estimate_at REAL
);
CREATE INDEX IF NOT EXISTS rounds_session ON rounds (session, state);
"""
# The states a run stays in while it can go on (waiting: from before 0.6.7)
OPEN = ("running", "waiting")
# Columns renamed since a database was made (0.6.7: a budget the user
# approved became ZERO's estimate), renamed in place when it opens
RENAMED_COLUMNS = {"rounds": {
    "budget_rounds": "estimate_rounds", "budget_minutes": "estimate_minutes",
    "budget_dollars": "estimate_dollars",
}}
# Columns added since a database was made (0.6.7; 0.6.8: ZERO's estimate
# and use in weighted tokens, beside the dollars the screens show)
ADDED_COLUMNS = {"rounds": {
    "past_estimate_at": "REAL",
    "estimate_weighted_tokens": "REAL",
    "spent_weighted_tokens": "REAL NOT NULL DEFAULT 0",
}}


class RoundStore:
    """The rounds' rows, over the server's store."""

    def __init__(self, store):
        self.store = store

    def add(self, id, session, objective, done_check, estimate, dollars,
            inbox):
        """`estimate`: (rounds, minutes, weighted tokens); `dollars`: the
        estimate in the user's unit."""
        rounds, minutes, weighted_tokens = estimate
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO rounds (id, session, objective, done_check, state, "
                "estimate_rounds, estimate_minutes, estimate_weighted_tokens, "
                "estimate_dollars, round_inbox, started_at) "
                "VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)",
                (id, session, objective, done_check, rounds, minutes,
                 weighted_tokens, dollars, inbox, time.time()),
            )

    def run(self, id):
        rows = self.store.query("SELECT * FROM rounds WHERE id = ?", id)
        return rows[0] if rows else None

    def runs(self, session=None, open_only=False):
        """Runs, the oldest first: a session's, or every one; open ones only."""
        sql = "SELECT * FROM rounds WHERE 1"
        args = []
        if session is not None:
            sql += " AND session = ?"
            args.append(session)
        if open_only:
            sql += " AND state IN ('running', 'waiting')"
        return self.store.query(sql + " ORDER BY started_at", *args)

    def open_run(self, session):
        """The session's open run, or None: a session has one at a time."""
        found = self.runs(session, open_only=True)
        return found[-1] if found else None

    def update(self, id, notice=None, **fields):
        """A run's change, and the next round's message with it in one
        transaction. `notice`: (inbox id, text)."""
        names = ", ".join(f"{name} = ?" for name in fields)
        with self.store.transaction() as db:
            if fields:
                db.execute(f"UPDATE rounds SET {names} WHERE id = ?",
                           (*fields.values(), id))
            if notice is not None:
                msg_id, text = notice
                session = db.execute(
                    "SELECT session FROM rounds WHERE id = ?", (id,)
                ).fetchone()["session"]
                self.store.queue_notice(
                    db, msg_id, session, SOURCE, {"text": text, "run": id}
                )

    def withdraw(self, msg_id):
        """A round's message no turn took yet, withdrawn (a stop)."""
        self.store.withdraw(msg_id)
