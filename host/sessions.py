"""Session handling: the list and what the user does to a session.

The runtime (runtime.py) runs turns; this is the rest of what a client asks
of a session: the list (with its open ask's question, its news and what the
parts add, host/parts.py), its history, `set` (what its next turn runs with,
logged then as a switch note), its title, archive, seen and last (which session
a bare `zero` opens), and delete, to the trash. A part of the runtime's own
class, over its store, locks and logs.
"""

import json
from pathlib import Path

import settings
from core import history
from host import trash
from host.parts import Refused
from sessions import upgrade
from sessions.lock import Busy

TITLE_LENGTH = 60


def title(text):
    line = text.strip().splitlines()[0]
    return line if len(line) <= TITLE_LENGTH else line[: TITLE_LENGTH - 1] + "…"


class Sessions:
    def history(self, sid, after=0, limit=None, before=None):
        """The session's records after a seq (and before one, to page back),
        the newest `limit` of them, and how many came before those (the gap)."""
        if self.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        records = upgrade.migrate(self.log(sid).records(), time=upgrade.timestamp())
        records = [r for r in records if r["seq"] > after
                   and (before is None or r["seq"] < before)]
        gap = 0
        if limit is not None and len(records) > limit:
            gap = len(records) - limit
            records = records[gap:]
        return records, gap

    def set_session(self, sid, mode=None, model=None, workspace=None,
                    effort=None):
        """Change what a session runs with. Its next turn takes it; a
        turn running or waiting for an answer keeps its own. Returns what was
        set, and whether a turn now holds the old values."""
        if self.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        fields = {}
        if mode is not None:
            fields["mode"] = self.known_mode(mode)
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
        if effort is not None:
            efforts = settings.BY_NAME["effort"].choices
            if effort not in efforts:
                raise Refused(f"unknown effort {effort!r}; efforts: {efforts}")
            fields["effort"] = effort
        if not fields:
            raise Refused(
                "nothing to set: give a mode, a model, a workspace or an effort"
            )
        # The session's row has no effort: its switch and its log hold it
        columns = {k: v for k, v in fields.items() if k != "effort"}
        with self.lock:
            switch = json.loads(self.store.session(sid)["switch"] or "{}")
            self.store.update_session(
                sid, **columns, switch=json.dumps({**switch, **fields})
            )
        return {"set": fields, "next_turn": bool(self.pending(sid))}

    def pending(self, sid, row=None):
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
        news the user hasn't seen, and what the parts add (the jobs: its
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
                "why": ask["why"], "created": ask["created"],
            }
            row["archived"] = bool(row["archived"])
            row["unseen"] = row["end_seq"] > row["seen_seq"]
            result.append(row)
        for part in self.parts.values():
            part.annotate(result)
        for row in result:
            row["pending"] = self.pending(row["id"], row)
        return result

    def seen(self, sid, seq):
        """The user has seen the session up to `seq`: it is the one they last
        worked in (OpenCode's `viewed`)."""
        if self.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        self.store.seen(sid, seq)

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

    def delete(self, sid):
        """Move the session's log and its parts' files (the jobs' output) to
        the trash, then forget its rows. Refused while anything of it runs or
        waits to."""
        if self.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        parts = list(self.parts.values())
        with self.lock:
            if sid in self.runners or sid in self.store.waiting():
                raise Refused("a turn is running in this session; stop it first")
            for part in parts:
                why = part.refuse_delete(sid)
                if why:
                    raise Refused(why)
            lock = self.session_lock(sid)
            try:
                lock.acquire()
            except Busy as error:
                raise Refused(str(error)) from None
            try:
                files = [self.paths.sessions / f"{sid}.jsonl"]
                for part in parts:
                    files += part.session_files(sid)
                where = trash.move(self.paths, files)
                tables = [table for part in parts for table in part.tables]
                self.store.forget(sid, tables)
                self.live.pop(sid, None)
                self.blocked.discard(sid)
            finally:
                lock.release()
        (self.paths.locks / f"{sid}.lock").unlink(missing_ok=True)
        return where
