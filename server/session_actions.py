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
from datetime import datetime
from pathlib import Path

import settings
from core import history
from core.loop import Session
from server import trash
from server.services import Refused
from sessions import upgrade
from sessions.lock import Busy
from sessions.log import write_copy

TITLE_LENGTH = 60
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
    """Sortable by creation time and short enough to type."""
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def title(text):
    line = text.strip().splitlines()[0]
    return line if len(line) <= TITLE_LENGTH else line[: TITLE_LENGTH - 1] + "…"


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
        return history.context_meter(self.log(sid).branch())

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
        with self.lock:
            switch = json.loads(self.store.session(sid)["switch"] or "{}")
            self.store.update_session(
                sid, **columns, switch=json.dumps({**switch, **fields})
            )
        return {"set": fields, "next_turn": bool(self.pending_switches(sid))}

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
        busy = sid in self.runners or row["state"] in ("running", "waiting")
        if not busy or not row["switch"]:
            return []
        switch = json.loads(row["switch"])
        return [name for name in history.SWITCHABLE if name in switch]

    def sessions(self, archived=None, workspace=None):
        """The session list, each with its open ask's question, whether it has
        news the user hasn't seen, and what the services add (the jobs: its
        running ones). `archived`: None for all, or only those archived (True)
        or not (False); `workspace`: only the sessions in exactly that
        folder."""
        rows = self.store.sessions()
        asks = {a["session"]: a for a in self.store.open_asks()}
        result = []
        for row in rows:
            if archived is not None and bool(row["archived"]) != archived:
                continue
            if workspace is not None and row["workspace"] != workspace:
                continue
            ask = asks.get(row["id"])
            row["ask"] = None if ask is None else {
                "id": ask["id"], "tool": ask["tool"], "command": ask["command"],
                "why": ask["why"], "created_at": ask["created_at"],
            }
            row["archived"] = bool(row["archived"])
            row["unseen"] = row["end_seq"] > row["seen_seq"]
            result.append(row)
        for service in self.services.values():
            service.annotate(result)
        for row in result:
            row["pending"] = self.pending_switches(row["id"], row)
        return result

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
        start a turn, newest first: where a rewind or a fork begins."""
        if self.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        records = self.log(sid).branch()
        return [{"seq": r["seq"], "time": r["time"], "text": r["text"]}
                for r in reversed(history.rewind_points(records))]

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

    def refuse_if_busy(self, sid, what):
        """Refused while a turn of the session runs, waits for an answer or
        waits to start, or a service has work of it open (a run in rounds)."""
        row = self.store.session(sid)
        if (sid in self.runners or sid in self.store.waiting_sessions()
                or row["state"] in ("running", "waiting")):
            raise Refused(f"a turn is running or waiting in this session; {what} "
                          "once it ends")
        for service in self.services.values():
            why = service.refuse_rewind(sid)
            if why:
                raise Refused(why)

    def rewind(self, sid, seq):
        """Go back to before the user's message `seq` on the current branch
        (core/loop.py's Session.rewind): what came after is left behind,
        kept in the log, and the files and memory stay as they are. Refused
        while anything of the session runs or waits to. Returns the rewind's
        record and the message's text, for the user's input."""
        if self.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        with self.lock:
            self.refuse_if_busy(sid, "rewind")
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
                    self.live.pop(sid, None)
                    session = Session(None, [], "", log, {})
                try:
                    record, left = session.rewind(seq, sid)
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
        parent goes on untouched. Returns the new session's row and the
        message's text for its input ("" for all of it)."""
        info = self.store.session(sid)
        if info is None:
            raise Refused(f"no session {sid!r}")
        records = self.log(sid).branch()
        if history.log_format(records) != history.LOG_FORMAT:
            raise Refused("this session's log is from an older ZERO; "
                          "send it a message first")
        if seq is None:
            if (sid in self.runners or info["state"] in ("running", "waiting")
                    or history.unfinished_turn(records)):
                raise Refused("its last turn hasn't ended; fork from one of the "
                              "user's messages, or once it ends")
            copied, text, went_on = records, "", None
        else:
            try:
                _, left = history.left_behind_by(records, seq)
            except ValueError as error:
                raise Refused(str(error)) from None
            copied = records[: records.index(left[0])]
            text = left[0]["text"]
            # What the parent had done past the fork's point, for its note
            went_on = {"first": left[0]["seq"], "last": left[-1]["seq"],
                       "part": history.part_lines(left)}
        new = new_id()
        made = datetime.now().astimezone().isoformat(timespec="seconds")
        earlier = copied[0].get("forked_from") or {}
        header = {**copied[0], "forked_from": {
            "session": sid, "seq": copied[-1]["seq"], "title": info["title"],
            "time": made,
            # Every session it came from, the parent first: their saved
            # outputs, which its copied context names, stay readable
            "ancestors": [sid, *earlier.get("ancestors", [])],
            **({"went_on": went_on} if went_on else {}),
        }}
        write_copy(self.paths.sessions / f"{new}.jsonl", [header, *copied[1:]])
        switch = self.carried_switch(sid, records, copied)
        self.store.add_session(new, {
            "title": info["title"], "source": source, "model": info["model"],
            "mode": info["mode"], "workspace": info["workspace"],
            "switch": json.dumps(switch) if switch else None, "parent": sid,
            "seen_seq": copied[-1]["seq"],
        })
        return {"session": self.store.session(new), "text": text}

    def delete(self, sid):
        """Move the session's log and its services' files (the jobs' output) to
        the trash, then forget its rows. Refused while anything of it runs or
        waits to."""
        if self.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        services = list(self.services.values())
        with self.lock:
            if sid in self.runners or sid in self.store.waiting_sessions():
                raise Refused("a turn is running in this session; stop it first")
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
                self.live.pop(sid, None)
                self.blocked.discard(sid)
            finally:
                lock.release()
        (self.paths.locks / f"{sid}.lock").unlink(missing_ok=True)
        return where
