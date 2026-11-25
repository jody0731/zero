"""Session handling: the list and what the user does to a session.

The runtime (runtime.py) runs turns; this is the rest of what a client asks
of a session: the list (with its open ask's question, its news and what the
services add, server/services.py), its history, `set` (what its next turn runs
with, logged then as a switch note), its title, archive, seen and last (which
session a bare `zero` opens), delete, to the trash, and the conversation's
tree: a rewind to before one of the user's messages, and a fork into a new
session. A service of the runtime's own class, over its store, locks and logs.
"""

import copy
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from functools import partial
from pathlib import Path

import settings
from core import attribution, entries, replay, timeline
from core.loop import Session
from server import replies, trash
from server.rebuild import header_row
from server.services import Refused
from sessions import upgrade
from sessions.lock import Busy
from sessions.entries import fork_header
from sessions.log import write_copy

TITLE_LENGTH = 60
# How many new ids a new session tries when one is taken
ID_TRIES = 5
# What a session's row holds of what it runs with; the rest is in its log
COLUMNS = ("mode", "model", "workspace")


def check_switchable(name, value):
    """Validate and normalize a switchable setting value, or raise Refused."""
    setting = settings.BY_NAME.get(name)
    if setting is None or not setting.switchable:
        raise Refused(f"cannot switch {name!r}")
    if setting.empty_means_none:
        if not isinstance(value, str):
            raise Refused(f"{name} must be a string")
        stripped = value.strip()
        if not stripped:
            return None
        value = stripped
    problem = settings.check(setting, value)
    if problem:
        raise Refused(problem)
    return value


def new_id():
    """Sortable by creation time and short enough to type; 8 random hex
    digits, so sessions made in the same second don't collide (4 did at 200
    sessions made at once, 7.5.1); one that does is made again (its
    callers)."""
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"


def title(text):
    lines = text.strip().splitlines()
    if not lines:
        return ""
    line = lines[0]
    return line if len(line) <= TITLE_LENGTH else line[: TITLE_LENGTH - 1] + "…"


class SessionActions:
    def history(self, sid, after=0, limit=None, before=None):
        """The session's records after a seq (and before one, to page back),
        the newest `limit` of them, and how many came before those (earlier_count)."""
        if self.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        records = self.read_branch(sid)
        records = [
            r for r in replay.transcript(records)
            if not entries.has(r.entry, "private_continuation")
            and r.seq > after and (before is None or r.seq < before)
        ]
        earlier_count = 0
        if limit is not None and len(records) > limit:
            earlier_count = len(records) - limit
            records = records[earlier_count:]
        return records, earlier_count

    def with_origins(self, sid, records):
        return attribution.attributed_records(records, sid, self.read_header)

    def record_sessions(self, sid, records):
        if not records:
            return ()
        return attribution.record_sessions(
            records, sid, self.read_header, header=self.read_header(sid),
        )

    def context_meter(self, sid, *, log=None):
        """The session's context meter, or None before its first call."""
        log = log or self.read_log(sid)
        compact_at = self.meter_line()
        try:
            return log.context_meter(compact_at)
        except timeline.OldLog:
            return timeline.context_meter(self.read_branch(sid, log=log), compact_at)

    def logged_switches(self, sid, log=None):
        log = log or self.read_log(sid)
        try:
            return log.running_with()
        except timeline.OldLog:
            return timeline.running_with(self.read_branch(sid, log=log))

    def set_session(self, sid, mode=None, model=None, workspace=None, **overrides):
        """Change what a session runs with. Its next turn takes it; a
        turn running or waiting for an answer keeps its own. Returns what
        was set, and whether a turn now holds the old values."""
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
        for name, value in overrides.items():
            if value is None:
                continue
            fields[name] = check_switchable(name, value)
        if not fields:
            names = ", ".join(timeline.SWITCHABLE)
            raise Refused(f"nothing to set: give one of {names}")
        # Non-column switchable settings are carried by switch and the log
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
        """What the session's next turn runs with: its core columns, its
        logged switchable settings, and any pending switch."""
        row = self.store.session(sid)
        if row is None:
            raise Refused(f"no session {sid!r}")
        config = self.config()
        now = {
            name: config.get(name, settings.DEFAULTS.get(name))
            for name in timeline.SWITCHABLE
            if name not in COLUMNS
        }
        now.update(self.logged_switches(sid))
        now.update(json.loads(row["switch"] or "{}"))
        now.update({name: row[name] for name in COLUMNS})
        for name in timeline.SWITCHABLE:
            setting = settings.BY_NAME.get(name)
            if (now.get(name) is None and setting is not None
                    and setting.none_means_default):
                now[name] = setting.default
        return {name: now[name] for name in timeline.SWITCHABLE}

    def pending_switches(self, sid, row=None):
        """What a running or waiting turn holds from before a `set`: those
        values change when its next turn starts."""
        row = row or self.store.session(sid)
        if not row or not row["switch"]:
            return []
        if row["state"] not in ("running", "waiting"):
            return []
        switch = json.loads(row["switch"])
        return [name for name in timeline.SWITCHABLE if name in switch]

    def sessions(self, archived=None, workspace=None):
        """The session list, each with its open ask's question, whether it has
        news the user hasn't seen, and what the services add (the jobs: its
        running ones). `archived`: None for all, or only those archived (True)
        or not (False); `workspace`: only the sessions in exactly that
        folder."""
        return self.sessions_snapshot(archived, workspace)[1]

    def copy_row(self, row):
        return {
            key: (copy.deepcopy(value) if isinstance(value, (dict, list)) else value)
            for key, value in row.items()
        }

    def sessions_snapshot(self, archived=None, workspace=None, min_version=None):
        """Return `(version, sorted_rows)` from the incrementally refreshed
        session-list cache, filtered by `(archived, workspace)`."""
        self.ensure_list_cache(min_version)
        with self.list_cache_lock:
            version = self.list_cache_version
            rows = [
                self.copy_row(row) for row in self.list_cache_rows.values()
                if (archived is None or row["archived"] == archived)
                and (workspace is None or row["workspace"] == workspace)
            ]
        rows.sort(
            key=lambda row: (
                row["state"] == "waiting",
                row["updated_at"],
                row["created_at"],
            ),
            reverse=True,
        )
        return version, rows

    def session_rows_for(self, sids, min_version=None):
        """Return `{sid: row_or_None}` for the named sessions from the
        incrementally refreshed cache."""
        self.ensure_list_cache(min_version)
        with self.list_cache_lock:
            return {
                sid: (
                    self.copy_row(row)
                    if (row := self.list_cache_rows.get(sid)) is not None
                    else None
                )
                for sid in sids
            }

    def ensure_list_cache(self, min_version=None):
        """Refresh only the sessions changed since `list_cache_version` (or do
        a full build on cold start or ring-buffer overflow) until the cache
        reaches at least `min_version` (default: current `list_version`)."""
        want = self.list_version if min_version is None else min_version
        with self.list_cache_lock:
            cached_ver = self.list_cache_version
            if cached_ver is not None and cached_ver >= want:
                return cached_ver
        with self.list_build_lock:
            while True:
                with self.list_cache_lock:
                    cached_ver = self.list_cache_version
                    if cached_ver is not None and cached_ver >= want:
                        return cached_ver
                if cached_ver is None:
                    target = self.list_version
                    built = self._build_rows_map(None)
                    with self.list_cache_lock:
                        self.list_cache_rows = built
                        self.list_cache_version = target
                        if target >= want:
                            return target
                else:
                    target, touched = self.watcher_hub.list_changes_since(cached_ver)
                    if touched is None:
                        built = self._build_rows_map(None)
                        with self.list_cache_lock:
                            self.list_cache_rows = built
                            self.list_cache_version = target
                            if target >= want:
                                return target
                    elif not touched:
                        with self.list_cache_lock:
                            self.list_cache_version = max(cached_ver, target)
                            if self.list_cache_version >= want:
                                return self.list_cache_version
                    else:
                        updated = self._build_rows_map(touched)
                        with self.list_cache_lock:
                            for sid in touched:
                                if sid in updated:
                                    self.list_cache_rows[sid] = updated[sid]
                                else:
                                    self.list_cache_rows.pop(sid, None)
                            self.list_cache_version = target
                            if target >= want:
                                return target

    def _build_rows_map(self, sids=None):
        if sids is None:
            stored = self.store.sessions()
            asks = {a["session"]: a for a in self.store.open_asks()}
        else:
            stored = self.store.sessions_by_ids(sids)
            asks = {a["session"]: a for a in self.store.open_asks_for(sids)}
        rows = [
            replies.session_row(
                row, asks.get(row["id"]), self.pending_switches(row["id"], row),
            )
            for row in stored
        ]
        if rows:
            for service in self.services.values():
                service.annotate(rows)
        return {row["id"]: row for row in rows}

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
        records = self.read_branch(sid)
        after = self.copied_to(sid)
        found = []
        for r in reversed(timeline.rewind_points(records)):
            if r.seq <= after:
                continue
            text = entries.text(r.entry)
            label = text if text.strip() else (self.message_label(r.entry) or text)
            item = {"seq": r.seq, "time": r.time, "text": label}
            if restored := self.message_restore(r.entry):
                item.update(restored)
            found.append(item)
        return found

    def copied_to(self, sid):
        """The last record a session ZERO started copied from its parent (a
        subagent's fork), else 0: its rewind points start after it."""
        if self.store.session(sid).get("started_by") != "zero":
            return 0
        head = self.read_header(sid)
        return (head.entry.get("forked_from") or {}).get("seq", 0) if head else 0

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
        old, new = timeline.running_with(before), timeline.running_with(after)
        now = {**old, **{name: row[name] for name in COLUMNS}}
        changed = {name: now[name] for name in timeline.SWITCHABLE
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
                try:
                    before = log.branch()
                except timeline.OldLog:
                    before = ()
                if timeline.log_format(before) != timeline.LOG_FORMAT:
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
                    record, left = session.rewind(seq)
                except ValueError as error:
                    raise Refused(str(error)) from None
                switch = self.carried_switch(sid, before, log.branch())
                self.store.update_session(
                    sid, switch=json.dumps(switch) if switch else None
                )
            finally:
                lock.release()
        restored = self.message_restore(left[0].entry)
        return {"rewind": record, "text": entries.text(left[0].entry), **restored}

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
        log = self.read_log(sid)
        try:
            records = log.branch()
        except timeline.OldLog:
            records = ()
        if timeline.log_format(records) != timeline.LOG_FORMAT:
            raise Refused("this session's log is from an older ZERO; "
                          "send it a message first")
        if seq is None:
            state = self.states.get(sid)
            if ((state is not None and state.runner is not None)
                    or info["state"] in ("running", "waiting")
                    or log.unfinished_turn()):
                raise Refused("its last turn hasn't ended; fork from one of the "
                              "user's messages, or once it ends")
            copied, text, went_on, restored = records, "", None, {}
        else:
            self.refuse_copied(sid, seq)
            try:
                _, left = timeline.left_behind_by(records, seq)
            except ValueError as error:
                raise Refused(str(error)) from None
            copied = records[: records.index(left[0])]
            text = entries.text(left[0].entry)
            restored = self.message_restore(left[0].entry)
            went_on = {"effects": "".join(
                service.fork_effects(sid, left) for service in self.services.values()
            )}
        new = self.copy_session(sid, info, records, copied, went_on,
                                {"title": info["title"], "source": source,
                                 "started_by": "user"})
        return {"session": replies.session_row(self.store.session(new)),
                "text": text, **restored}

    def fork_finished(self, sid, title, source, rows_beside=None):
        """A new session from this one's finished turns: its current branch
        up to the start of the turn under way (all of it, when none is), as
        `fork` copies; ZERO's, a subagent's fork made mid-turn (DSH's "the
        parent's completed conversation turns"). `rows_beside(db, id, parent=...)`:
        rows its maker writes in the same transaction as its own (a subagent's).
        Returns the new session's id."""
        info = self.store.session(sid)
        if info is None:
            raise Refused(f"no session {sid!r}")
        log = self.read_log(sid)
        try:
            records = log.branch()
        except timeline.OldLog:
            records = ()
        if timeline.log_format(records) != timeline.LOG_FORMAT:
            raise Refused("this session's log is from an older ZERO")
        under_way = log.unfinished_turn()
        copied = records[: len(records) - len(under_way)]
        if rows_beside is not None:
            rows_beside = partial(rows_beside, parent=info)
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
            "seen_seq": copied[-1].seq, "created_at": made.timestamp(),
        }
        earlier = copied[0].entry.get("forked_from") or {}
        forked_from = {
            "session": sid, "seq": copied[-1].seq, "title": info["title"],
            "time": made.isoformat(timespec="seconds"),
            # Every session it came from, the parent first: their saved
            # outputs, which its copied context names, stay readable
            "ancestors": [sid, *earlier.get("ancestors", [])],
            "ancestor_cutoffs": attribution.ancestor_cutoffs(
                sid, copied[0], self.read_header, self.read_records,
            ),
            "started_by": row["started_by"],
            **({"went_on": went_on} if went_on else {}),
        }
        header = fork_header(copied[0], copied[-1].entry, forked_from, header_row(full))
        for tries in range(1, ID_TRIES + 1):
            new = new_id()
            path = self.paths.sessions / f"{new}.jsonl"
            copied_for_child = list(copied)
            for service in self.services.values():
                copied_for_child = service.rewrite_fork_records(
                    sid, new, copied_for_child,
                )
            try:
                parent_records = self.with_origins(sid, copied_for_child)
                next(parent_records)
                write_copy(path, [header, *parent_records])
            except FileExistsError:
                continue

            def fork_rows(db, child_id):
                if rows_beside is not None:
                    rows_beside(db, child_id)
                for service in self.services.values():
                    service.session_forked(db, sid, child_id, copied_for_child)

            try:
                self.store.add_session(new, full, fork_rows)
            except sqlite3.IntegrityError:
                path.unlink(missing_ok=True)
                if tries == ID_TRIES:
                    raise
                continue
            for service in self.services.values():
                service.after_fork(new, copied_for_child)
            return new
        raise FileExistsError(f"{ID_TRIES} new session ids were all taken")

    def new_child(self, sid, title, source, rows_beside=None,
                  mode=None, model=None, *, parent_log=None):
        """A new session under this one, its log empty: the same workspace,
        mode and model unless explicitly selected, and the thinking effort
        the parent runs with, which its first turn takes as a switch.
        `rows_beside(db, id, parent=...)`: rows its maker writes in the transaction
        as its own. An in-process parent can supply the log its turn owns.
        Returns the child's ID."""
        info = self.store.session(sid)
        if info is None:
            raise Refused(f"no session {sid!r}")
        effort = self.logged_switches(sid, parent_log).get("thinking_effort")
        row = {
            "title": title, "source": source, "model": model or info["model"],
            "mode": mode or info["mode"], "workspace": info["workspace"],
            "switch": json.dumps({"thinking_effort": effort}) if effort else None,
            "parent": sid, "started_by": "zero",
        }
        if rows_beside is not None:
            rows_beside = partial(rows_beside, parent=info)
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
                for service in services:
                    service.before_trash(sid)
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
