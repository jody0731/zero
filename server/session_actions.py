"""Session handling: the list and what the user does to a session.

The runtime (runtime.py) runs turns; this is the rest of what a client asks
of a session: the list (with its open ask's question, its news and what the
services add, server/services.py), its history, `set` (what its next turn runs
with, logged then as a switch note), its title, archive, seen and last (which
session a bare `zero` opens), delete, to the trash, and the conversation's
tree: a rewind to before one of the user's messages, and a fork into a new
session. A service of the runtime's own class, over its store, locks and logs.
"""

import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import settings
from core import history
from core.loop import Session
from server import trash
from server.rebuild import header_row
from server.services import Refused
from sessions import upgrade
from sessions.lock import Busy
from sessions.log import write_copy

TITLE_LENGTH = 60
# How many new ids a new session tries when one is taken
ID_TRIES = 5
# What a session's row holds of what it runs with; the rest is in its log
COLUMNS = ("mode", "model", "workspace")


def check_thinking_effort(name, effort):
    """`effort` if it is one of the setting's choices; else refused."""
    efforts = settings.BY_NAME[name].choices
    if effort not in efforts:
        what = "judge effort" if name == "judge_thinking_effort" else "thinking_effort"
        raise Refused(f"unknown {what} {effort!r}; efforts: {efforts}")
    return effort


def new_id():
    """Sortable by creation time and short enough to type; 8 random hex
    digits, so sessions made in the same second don't collide (4 did at 200
    sessions made at once, 7.5.1); one that does is made again (its
    callers)."""
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"


def title(text):
    line = text.strip().splitlines()[0]
    return line if len(line) <= TITLE_LENGTH else line[: TITLE_LENGTH - 1] + "…"


def asked(ask):
    """An open ask's row as apps read it: its question, its parts (the
    reason the user reads, what it is about) and the rule's words with
    the reason (`why`, which older apps draw)."""
    return {name: ask[name] for name in (
        "id", "tool", "command", "why", "reason", "about", "created_at")}


class SessionActions:
    def history(self, sid, after=0, limit=None, before=None):
        """The session's records after a seq (and before one, to page back),
        the newest `limit` of them, and how many came before those (earlier_count)."""
        if self.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        records = upgrade.migrate(self.log(sid).branch(), time=upgrade.timestamp())
        records = [r for r in records if r["seq"] > after
                   and (before is None or r["seq"] < before)]
        earlier_count = 0
        if limit is not None and len(records) > limit:
            earlier_count = len(records) - limit
            records = records[earlier_count:]
        return records, earlier_count

    def context_meter(self, sid):
        """The session's context as the user's meter shows it (core/history.py's
        context_meter), or None before its first call."""
        return history.context_meter(self.log(sid).branch(), self.meter_line())

    def set_session(self, sid, mode=None, model=None, workspace=None,
                    thinking_effort=None, judge_model=None, judge_thinking_effort=None):
        """Change what a session runs with. Its next turn takes it; a
        turn running or waiting for an answer keeps its own. `judge_model`
        "": the session's own model again. Returns what was set, and whether
        a turn now holds the old values."""
        if self.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        fields = {}
        if mode is not None:
            fields["mode"] = self.check_mode(mode)
        if model is not None:
            if not model.strip():
                raise Refused("a model needs a name")
            fields["model"] = model.strip()
        if workspace is not None:
            path = Path(workspace)
            if not path.is_absolute():
                raise Refused(f"a workspace is an absolute folder, not {workspace!r}")
            if not path.is_dir():
                raise Refused(f"no folder {workspace}")
            fields["workspace"] = str(path.resolve())
        if thinking_effort is not None:
            fields["thinking_effort"] = check_thinking_effort(
                "thinking_effort", thinking_effort
            )
        if judge_model is not None:
            if not isinstance(judge_model, str):
                raise Refused("a judge model is a model's name, or empty for "
                              "the session's own")
            fields["judge_model"] = judge_model.strip() or None
        if judge_thinking_effort is not None:
            fields["judge_thinking_effort"] = check_thinking_effort(
                "judge_thinking_effort", judge_thinking_effort
            )
        if not fields:
            raise Refused(
                "nothing to set: give a mode, a model, a workspace, a thinking effort, "
                "or the judge's model or thinking effort"
            )
        # The session's row has no effort or judge: its switch and its log
        # hold them
        columns = {k: v for k, v in fields.items() if k in COLUMNS}
        with self.state(sid).lock:
            switch = json.loads(self.store.session(sid)["switch"] or "{}")
            self.store.update_session(
                sid, **columns, switch=json.dumps({**switch, **fields})
            )
        return {"set": fields, "next_turn": bool(self.pending_switches(sid))}

    def live_settings(self):
        """ZERO's settings, each read as it stands at its use (its default
        where the config lacks it)."""
        return settings.LiveSettings(self.config)

    def setting(self, name):
        """One of ZERO's settings as it stands now."""
        return self.live_settings()[name]

    def runs_with(self, sid):
        """What the session's next turn runs with: its mode, model and
        workspace (its row), its effort and judge (its log, then a switch
        still to come). What the log doesn't name yet is config.toml's."""
        row = self.store.session(sid)
        if row is None:
            raise Refused(f"no session {sid!r}")
        config = self.config()
        now = {
            "thinking_effort": config["thinking_effort"],
            "judge_model": config["judge_model"],
            "judge_thinking_effort": config["judge_thinking_effort"],
            **history.running_with(self.log(sid).branch()),
            **json.loads(row["switch"] or "{}"),
        }
        now.update({name: row[name] for name in COLUMNS})
        return {name: now[name] for name in history.SWITCHABLE}

    def pending_switches(self, sid, row=None):
        """What a running or waiting turn holds from before a `set`: those
        values change when its next turn starts."""
        row = row or self.store.session(sid)
        state = self.states.get(sid)
        busy = ((state is not None and state.runner is not None)
                or row["state"] in ("running", "waiting"))
        if not busy or not row["switch"]:
            return []
        switch = json.loads(row["switch"])
        return [name for name in history.SWITCHABLE if name in switch]

    def sessions(self, archived=None, workspace=None):
        """The session list, each with its open ask's question, whether it has
        news the user hasn't seen, and what the services add (the jobs: its
        running ones). `archived`: None for all, or only those archived (True)
        or not (False); `workspace`: only the sessions in exactly that
        folder. Built whole once for each change the database commits (the
        list's version), whoever asks for it meanwhile."""
        result = []
        for row in self.listed_rows():
            if archived is not None and row["archived"] != archived:
                continue
            if workspace is not None and row["workspace"] != workspace:
                continue
            row = dict(row)
            # Whether a turn holds a switch is the server's, not the database's
            row["pending"] = self.pending_switches(row["id"], row)
            result.append(row)
        return result

    def listed_rows(self):
        """Every session's row as the list shows it, built once for each
        version of the list: kept, never changed, by every reader of it."""
        version = self.list_version
        kept = getattr(self, "listed", None)
        if kept is not None and kept[0] == version:
            return kept[1]
        rows = self.store.sessions()
        asks = {a["session"]: a for a in self.store.open_asks()}
        for row in rows:
            ask = asks.get(row["id"])
            row["ask"] = None if ask is None else asked(ask)
            row["archived"] = bool(row["archived"])
            row["unseen"] = row["end_seq"] > row["seen_seq"]
        for service in self.services.values():
            service.annotate(rows)
        # A change committed while it was built moved the version on: the
        # next reader builds it again
        self.listed = (version, rows)
        return rows

    def mark_seen(self, sid, seq):
        """The user has seen the session up to `seq`: it is the one they last
        worked in (OpenCode's `viewed`)."""
        if self.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        self.store.mark_seen(sid, seq)

    def last(self):
        return self.store.last_seen()

    def rename(self, sid, text):
        if self.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        if not text.strip():
            raise Refused("a title can't be empty")
        self.store.update_session(sid, title=title(text))

    def archive(self, sid, archived=True):
        """Hidden from the list unless asked for; it still runs and answers."""
        if self.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        self.store.update_session(sid, archived=int(archived))

    def rewind_points(self, sid):
        """The user's own messages on the conversation's current branch that
        start a turn, newest first: where a rewind or a fork begins. A
        session ZERO started has none in what it copied from its parent: a
        rewind there would leave its task behind."""
        if self.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        records = self.log(sid).branch()
        after = self.copied_to(sid)
        return [{"seq": r["seq"], "time": r["time"], "text": r["text"]}
                for r in reversed(history.rewind_points(records))
                if r["seq"] > after]

    def copied_to(self, sid):
        """The last record a session ZERO started copied from its parent (a
        subagent's fork), else 0: its rewind points start after it."""
        if self.store.session(sid).get("started_by") != "zero":
            return 0
        head = self.log(sid).first_record() or {}
        return (head.get("forked_from") or {}).get("seq", 0)

    def refuse_copied(self, sid, seq):
        """Refused for a record a subagent copied from its parent."""
        if seq <= self.copied_to(sid):
            raise Refused(f"record {seq} is in what this session copied from "
                          "the session that started it; its rewind points "
                          "start after its task")

    def carried_switch(self, sid, before, after):
        """What the part of branch `before` that branch `after` lacks had
        changed of what the session runs with (mode, model, workspace,
        effort, judge), as it stands now (its row), and a switch still to
        come: a switch for the next turn, so a rewind or a fork goes on as
        the user left it, and tells the model so."""
        row = self.store.session(sid)
        pending = json.loads(row["switch"] or "{}")
        old, new = history.running_with(before), history.running_with(after)
        now = {**old, **{name: row[name] for name in COLUMNS}}
        changed = {name: now[name] for name in history.SWITCHABLE
                   if name in now and old.get(name) != new.get(name)}
        return {**changed, **pending}

    @contextmanager
    def held(self, sid, busy, states=()):
        """The session held as its runner, as a compaction holds it, so no
        turn starts while slow work on it (a harness built, files moved to
        the trash) runs outside the runtime's lock, and the services are
        asked outside it too (design rule 12). Refused with `busy` while a
        turn of it runs or waits to start, or its row is in `states`; a
        message admitted meanwhile starts its turn after."""
        if self.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        state = self.state(sid)
        with state.lock:
            row = self.store.session(sid)
            if row is None:
                raise Refused(f"no session {sid!r}")
            if (state.runner is not None or self.store.has_queued(sid)
                    or row["state"] in states):
                raise Refused(busy)
            self.hold_runner(state, threading.current_thread())
        try:
            yield row
        finally:
            with state.lock:
                self.let_go(state, leaving=False)
            if self.started:
                self.dispatch(sid)

    def rewind(self, sid, seq):
        """Go back to before the user's message `seq` on the current branch
        (core/loop.py's Session.rewind): what came after is left behind,
        kept in the log, and the files and memory stay as they are. Refused
        while anything of the session runs or waits to. Returns the rewind's
        record and the message's text, for the user's input."""
        busy = ("a turn is running or waiting in this session; rewind once "
                "it ends")
        with self.held(sid, busy, states=("running", "waiting")):
            for service in self.services.values():
                why = service.refuse_rewind(sid)
                if why:
                    raise Refused(why)
            lock = self.session_lock(sid)
            try:
                lock.acquire()
            except Busy as error:
                raise Refused(str(error)) from None
            try:
                log = self.log(sid, lock)
                before = log.branch()
                if history.log_format(before) != history.LOG_FORMAT:
                    raise Refused("this session's log is from an older ZERO; "
                                  "send it a message first")
                try:
                    session = self.harness(sid, log)
                except Exception:
                    # A rewind calls no model: a session whose own can't be
                    # built now (its server gone, no key) rewinds with no
                    # hooks, its note without their lines; a harness kept
                    # from before is built again from the log at its next use
                    self.state(sid).harness = None
                    session = Session(None, [], "", log, {})
                self.refuse_copied(sid, seq)
                try:
                    record, left = session.rewind(
                        seq, self.setting("rewind_note_line_chars"), sid)
                except ValueError as error:
                    raise Refused(str(error)) from None
                switch = self.carried_switch(sid, before, log.branch())
                self.store.update_session(
                    sid, switch=json.dumps(switch) if switch else None
                )
            finally:
                lock.release()
        return {"rewind": record, "text": left[0]["text"]}

    def fork(self, sid, seq=None, source="user"):
        """A new session from this one's conversation: its current branch up
        to before the user's message `seq` (None: all of it, its last turn
        ended), copied record for record with their numbers, in a new log
        whose header names where it came from (`forked_from`); the same
        workspace, mode and model, its row's `parent` this session. The
        parent goes on untouched. The user's: started by the user, whatever
        the session it came from. Returns the new session's row and the
        message's text for its input ("" for all of it)."""
        info = self.store.session(sid)
        if info is None:
            raise Refused(f"no session {sid!r}")
        records = self.log(sid).branch()
        if history.log_format(records) != history.LOG_FORMAT:
            raise Refused("this session's log is from an older ZERO; "
                          "send it a message first")
        if seq is None:
            state = self.states.get(sid)
            if ((state is not None and state.runner is not None)
                    or info["state"] in ("running", "waiting")
                    or history.unfinished_turn(records)):
                raise Refused("its last turn hasn't ended; fork from one of the "
                              "user's messages, or once it ends")
            copied, text, went_on = records, "", None
        else:
            self.refuse_copied(sid, seq)
            try:
                _, left = history.left_behind_by(records, seq)
            except ValueError as error:
                raise Refused(str(error)) from None
            copied = records[: records.index(left[0])]
            text = left[0]["text"]
            # What the parent had done past the fork's point, for its note
            went_on = {"first": left[0]["seq"], "last": left[-1]["seq"],
                       "part": history.part_lines(
                           left, self.setting("rewind_note_line_chars"))}
        new = self.copy_session(sid, info, records, copied, went_on,
                                {"title": info["title"], "source": source,
                                 "started_by": "user"})
        return {"session": self.store.session(new), "text": text}

    def fork_finished(self, sid, title, source, rows_beside=None):
        """A new session from this one's finished turns: its current branch
        up to the start of the turn under way (all of it, when none is), as
        `fork` copies; ZERO's, a subagent's fork made mid-turn (DSH's "the
        parent's completed conversation turns"). `rows_beside(db, id)`: rows
        its maker writes in the same transaction as its own (a subagent's).
        Returns the new session's id."""
        info = self.store.session(sid)
        if info is None:
            raise Refused(f"no session {sid!r}")
        records = self.log(sid).branch()
        if history.log_format(records) != history.LOG_FORMAT:
            raise Refused("this session's log is from an older ZERO")
        under_way = history.unfinished_turn(records)
        copied = records[: len(records) - len(under_way)]
        return self.copy_session(sid, info, records, copied, None,
                                 {"title": title, "source": source,
                                  "started_by": "zero"}, rows_beside)

    def copy_session(self, sid, info, records, copied, went_on, row,
                     rows_beside=None):
        """Write a fork: `copied` (the start of branch `records`) into a new
        log, its header naming where it came from and what the parent did
        past it (`went_on`), and its row: `row`'s title, source and who
        started it, the parent's workspace, mode and model, what the copied
        part lacks of what the parent runs with as a switch. Its id; one
        taken (a log or a row) is made again."""
        made = datetime.now().astimezone()
        switch = self.carried_switch(sid, records, copied)
        full = {
            **row, "model": info["model"], "mode": info["mode"],
            "workspace": info["workspace"],
            "switch": json.dumps(switch) if switch else None, "parent": sid,
            "seen_seq": copied[-1]["seq"], "created_at": made.timestamp(),
        }
        earlier = copied[0].get("forked_from") or {}
        header = {**copied[0], "forked_from": {
            "session": sid, "seq": copied[-1]["seq"], "title": info["title"],
            "time": made.isoformat(timespec="seconds"),
            # Every session it came from, the parent first: their saved
            # outputs, which its copied context names, stay readable
            "ancestors": [sid, *earlier.get("ancestors", [])],
            "started_by": row["started_by"],
            **({"went_on": went_on} if went_on else {}),
        }, "row": header_row(full)}
        for tries in range(1, ID_TRIES + 1):
            new = new_id()
            path = self.paths.sessions / f"{new}.jsonl"
            try:
                write_copy(path, [header, *copied[1:]])
            except FileExistsError:
                continue
            try:
                self.store.add_session(new, full, rows_beside)
            except sqlite3.IntegrityError:
                path.unlink(missing_ok=True)
                if tries == ID_TRIES:
                    raise
                continue
            return new
        raise FileExistsError(f"{ID_TRIES} new session ids were all taken")

    def new_child(self, sid, title, source, rows_beside=None):
        """A new session under this one, its log empty: the same workspace,
        mode and model, and the thinking effort the parent runs with, which
        its first turn takes as a switch; ZERO's, a subagent's handoff.
        `rows_beside(db, id)`: rows its maker writes in the same transaction
        as its own. Returns its id."""
        info = self.store.session(sid)
        if info is None:
            raise Refused(f"no session {sid!r}")
        effort = history.running_with(self.log(sid).branch()).get("thinking_effort")
        row = {
            "title": title, "source": source, "model": info["model"],
            "mode": info["mode"], "workspace": info["workspace"],
            "switch": json.dumps({"thinking_effort": effort}) if effort else None,
            "parent": sid, "started_by": "zero",
        }
        for tries in range(1, ID_TRIES + 1):
            new = new_id()
            try:
                self.store.add_session(new, row, rows_beside)
            except sqlite3.IntegrityError:
                if tries == ID_TRIES:
                    raise
                continue
            return new

    def delete(self, sid):
        """Move the session's log and its services' files (the jobs' output) to
        the trash, then forget its rows. Refused while anything of it runs or
        waits to."""
        services = list(self.services.values())
        with self.held(sid, "a turn is running in this session; stop it first"):
            for service in services:
                why = service.refuse_delete(sid)
                if why:
                    raise Refused(why)
            lock = self.session_lock(sid)
            try:
                lock.acquire()
            except Busy as error:
                raise Refused(str(error)) from None
            try:
                files = [self.paths.sessions / f"{sid}.jsonl"]
                for service in services:
                    files += service.session_files(sid)
                where = trash.move(self.paths, files)
                tables = [table for service in services for table in service.tables]
                self.store.forget(sid, tables)
            finally:
                lock.release()
        # What the server kept of it goes: its state (its watches told), its
        # scratch folder, and what each service kept (a reader of its log)
        self.forget_state(sid)
        forget = getattr(self.make_session, "forget", None)
        if forget is not None:
            forget(sid)
        for service in services:
            service.session_deleted(sid)
        (self.paths.locks / f"{sid}.lock").unlink(missing_ok=True)
        return where
