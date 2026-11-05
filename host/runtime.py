"""The runtime as a library: messages in, turns run, records out.

A message is admitted into the inbox before its sender hears back. Each session
with queued messages gets a runner thread that runs its turns one at a time,
oldest first; different sessions run side by side. The harness writes every
turn's history to the session log. The host's server (host/server.py) is a
thin shell around this; tests drive it directly.

The order of writes, so a crash anywhere leaves something the boot repair can
finish: admit (the inbox row) → ack; the `user` record → the row delivered and
the session running; the `turn_end` record → the row done and the session idle
or failed. A turn that parks on an ask: the `ask` record → its `asks` row
and the session waiting, the turn's row still delivered; the session's next
message is the reply: the `answer` record → the ask closed and the reply's row
done. A background job's end, check-in or loss is a `job` message written with
its row (jobs/host.py): an idle session starts a turn on it, a running
turn takes it in as a steer, a parked one keeps it until the ask is answered.

Each session has a workspace, a folder fixed when the session is made; none
means `/`, the whole machine. Every record a session's log appends is
handed to the session's watchers in memory, so a watch reads the log only once,
for its catch-up. busy() says what keeps the host up; a parked ask doesn't.
A clean stop marks the turns it cuts off, so they resume without using a try:
only a crash uses one (Pi's "a close is not a crash").

From Pi: a message sent while a turn runs either steers it or
waits as a follow-up until the turn answers, and `admit` says which became of
it; a stop gives the turn's untaken steers and follow-ups back to their sender;
`continue` takes a turn that ended without an answer on with a harness note;
`settled` tells a session's watchers that nothing more will happen on its own.
The host keeps what the user has seen of each session, and the list's
listeners hear each change to it (sessions.py).

The runtime names no component: the assembly hands in how a session is
made, the modes, and the parts (host/parts.py), which add their tables, messages,
reply step and share of each session.
"""

import json
import secrets
import threading
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import settings
from core import history
from core.control import Control
from core.loop import INTERRUPTED, Session
from core.asking import NO, PARK, YES
from sessions import upgrade
from sessions.log import Log, fresh_log
from sessions.lock import Busy, SessionLock
from host.parts import Refused
from host.sessions import Sessions, title
from host.store import Store, UnknownSession

FAILED = ("crashed", "cut_off", "failed")
MAX_RESUMES = 3
WHEN_BUSY = ("steer", "queue")
# What a turn begun by `continue` opens with: the user's wish, not their words
CONTINUE = "[The user asked you to go on with the task.]"


@dataclass
class Message:
    """A message from a client. `session` None starts a new session."""

    id: str
    text: str
    session: str | None = None
    mode: str | None = None
    model: str | None = None
    # Who sent it: each caller names itself (the CLI, terminal-<pid>)
    source: str = field(kw_only=True)
    # The ask this message replies to: the client marks a line typed at an
    # ask's prompt
    reply_to: str | None = None
    # One of the ask's choices, picked at the box: no consent check
    choice: str | None = None
    # A new session's workspace, an absolute folder; None means /
    workspace: str | None = None
    # While a turn runs: "steer" it, or "queue" as a follow-up after its answer
    when_busy: str = "steer"


def picked(ask, payload):
    """The choice picked at an ask's box, if the reply is marked for that
    ask and the ask offers it; else None."""
    choice = payload.get("choice")
    offered = ask.get("choices") or [YES, NO]
    if payload.get("reply_to") != ask["ask"] or choice not in offered:
        return None
    return choice


def switch_note(changes, session):
    """What the model reads when the user changed the session between turns:
    what changed, and what the plugins say it runs with now (the `switched`
    hook: Environment's workspace and sandbox)."""
    said = "; ".join(f"{name} {old} → {new}" for name, (old, new) in changes.items())
    now = session.hooks.collect("switched", session)
    return f"[Harness note: the user changed this session's {said}. From here on:{now}]"


class Watcher:
    """One watching connection's queue: records and events, in order."""

    def __init__(self):
        self.events = deque()
        self.ready = threading.Condition()
        # Why the host closes this watch: a `bye` reason, once it does
        self.bye = None

    def put(self, event):
        with self.ready:
            # A draft holds the whole reply so far, so a newer one replaces
            # one still queued: a slow reader gets the latest, not each
            if "draft" in event and self.events and "draft" in self.events[-1]:
                self.events[-1] = event
            else:
                self.events.append(event)
            self.ready.notify_all()

    def close(self, reason):
        with self.ready:
            self.bye = reason
            self.ready.notify_all()

    def take(self, timeout):
        """What came since the last take, waiting up to `timeout` for any."""
        with self.ready:
            self.ready.wait_for(lambda: self.events or self.bye, timeout)
            events = list(self.events)
            self.events.clear()
            return events


def new_id():
    """Sortable by creation time and short enough to type."""
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


class Runtime(Sessions):
    def __init__(self, paths, make_session, modes, parts=(), model=None,
                 mode=None, config=None, default_model=None):
        """`make_session(sid, info, log=None, ask=PARK)` builds a harness. An
        upgrade supplies a fresh log to obtain the current header without writes.
        `modes`: every mode's name, least to most permissive; a session's asks
        go to the clients following it (they park). `parts`: the components'
        (host/parts.py), attached once the store is open.
        `config()`: config.toml's settings as they stand (settings.Config), read
        for each new session: its mode and model unless the message names them.
        `model` and `mode` win over the config's. `default_model`: the model
        where neither the message nor the config names one."""
        paths.make()
        self.paths = paths
        self.modes = tuple(modes)
        if mode and mode not in self.modes:
            raise ValueError(f"unknown mode {mode!r}; modes: {self.modes}")
        self.store = Store(paths.db, [part.schema for part in parts if part.schema])
        self.config = config or (lambda: settings.DEFAULTS)
        self.make_session = make_session
        self._model = model
        self.default_model = default_model
        self._mode = mode
        # Harness sessions in memory, while the host runs: id → session
        self.live = {}
        # One runner thread per session with work: id → thread
        self.runners = {}
        self.running = {}
        self.blocked = set()
        # Watching connections: session id → {token: Watcher}
        self.watchers = {}
        # The reply streaming in, per session: its latest draft event
        self.drafts = {}
        self.watch_lock = threading.Lock()
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.accepting = True
        self.started = False
        # The session list's listeners wait for its version to move
        self.list_version = 0
        self.list_changed = threading.Condition()
        self.store.on_change = self.changed_list
        # The components' parts by name, the messages they add, and those of
        # them that only look
        self.parts = {}
        self.messages = {}
        self.looking = ()
        for part in parts:
            part.attach(self)
            self.parts[part.name] = part
            self.messages.update(part.messages())
            self.looking += tuple(part.looking)

    @property
    def model(self):
        """A new session's model, where its first message names none."""
        return self._model or self.config()["model"] or self.default_model

    @property
    def mode(self):
        return self._mode or self.config()["mode"]

    def known_mode(self, mode):
        """The mode, if the host has it; Refused if not."""
        if mode not in self.modes:
            raise Refused(f"unknown mode {mode!r}; modes: {self.modes}")
        return mode

    def changed_list(self):
        with self.list_changed:
            self.list_version += 1
            self.list_changed.notify_all()

    def wait_list(self, version, timeout):
        """The list's version once it passes `version`, or after `timeout`."""
        with self.list_changed:
            self.list_changed.wait_for(
                lambda: self.list_version != version, timeout
            )
            return self.list_version

    def wake(self):
        """A job's notice was admitted."""
        if self.started:
            self.dispatch()

    def log(self, sid, lock=None):
        """A session's log; what it appends reaches the session's watchers."""
        return Log(
            self.paths.sessions / f"{sid}.jsonl", lock=lock,
            on_append=lambda record: self.appended(sid, record),
        )

    def appended(self, sid, record):
        if record["type"] == "turn_end":
            self.store.ended(sid, record["seq"])
        # Any record ends the draft: the reply's own, a retry, a stop's note
        self.drafts.pop(sid, None)
        self.publish(sid, {"record": record})

    def draft(self, sid, step, draft):
        """The model's reply as it streams in (core/llm.py, Draft), to the
        session's watchers: the text so far, how much reasoning, the tools
        whose calls have begun. Kept until the next record, for a watch that
        starts mid-reply; never logged."""
        event = {"draft": {
            "step": step,
            "text": draft.content,
            "reasoning": len(draft.reasoning),
            "calls": [call["name"] for _, call in sorted(draft.calls.items())],
        }}
        self.drafts[sid] = event
        self.publish(sid, event)

    def current_draft(self, sid):
        """The draft event of the reply streaming in now, or None."""
        return self.drafts.get(sid)

    def session_lock(self, sid):
        return SessionLock(self.paths.locks / f"{sid}.lock")

    def admit(self, message, extra=None):
        """Commit a message to the inbox, then return the ack: its session and
        its disposition, what became of it. `extra`: payload fields."""
        if not self.accepting:
            raise Refused("the host is shutting down; send it again once it's back")
        if not message.text.strip():
            raise Refused("empty message")
        if message.when_busy not in WHEN_BUSY:
            raise Refused(f"when_busy is one of {WHEN_BUSY}")
        seen = self.store.query("SELECT session FROM inbox WHERE id = ?", message.id)
        if seen:
            return {"admitted": False, "session": seen[0]["session"]}
        new = None
        sid = message.session
        if sid is None:
            workspace = message.workspace or "/"
            if not Path(workspace).is_absolute():
                raise Refused(f"a workspace is an absolute folder, not {workspace!r}")
            if not Path(workspace).is_dir():
                raise Refused(f"no folder {workspace}")
            mode = self.known_mode(message.mode or self.mode)
            model = message.model or self.model
            if not model:
                raise Refused(
                    "no model is set yet: zero, the terminal, connects one "
                    "(Connect a model; later /settings, Models and keys), or "
                    "--model names one"
                )
            sid = new_id()
            new = {
                "title": title(message.text),
                "source": message.source,
                "model": model,
                "mode": mode,
                "workspace": str(Path(workspace).resolve()),
            }
        payload = {"text": message.text, **(extra or {})}
        if message.reply_to is not None:
            payload["reply_to"] = message.reply_to
            if message.choice is not None:
                payload["choice"] = message.choice
        with self.lock:
            disposition = "started" if new else self.disposition(sid, message)
            if disposition == "queued":
                payload["follow_up"] = True
            try:
                admitted, sid = self.store.admit(
                    message.id, sid, "user", message.source, payload,
                    new_session=new,
                )
            except UnknownSession:
                raise Refused(f"no session {sid!r}") from None
        if admitted and self.started:
            self.dispatch()
        return {"admitted": admitted, "session": sid, "disposition": disposition}

    def disposition(self, sid, message):
        """What becomes of a message to this session, decided under the lock:
        `reply` to its open ask, `steered` or `queued` (a follow-up) while a
        turn runs or waits to, `started` otherwise."""
        info = self.store.session(sid)
        if info is None:
            raise Refused(f"no session {sid!r}")
        if info["state"] == "waiting" and self.store.open_ask(sid) is not None:
            return "reply"
        busy = sid in self.runners or sid in self.store.waiting()
        if not busy:
            return "started"
        return "queued" if message.when_busy == "queue" else "steered"

    def continue_turn(self, sid, msg_id, source):
        """Take on a turn that ended without an answer (stopped, failed, past a
        limit), with no new message: the turn opens with a harness note. Refused
        while a turn runs, resumes or waits on an ask (Pi's `continue`)."""
        info = self.store.session(sid)
        if info is None:
            raise Refused(f"no session {sid!r}")
        seen = self.store.query("SELECT session FROM inbox WHERE id = ?", msg_id)
        if seen:
            return {"admitted": False, "session": sid}
        with self.lock:
            if sid in self.runners or sid in self.store.waiting():
                raise Refused("a turn is running in this session; nothing to continue")
            if self.store.open_ask(sid) is not None:
                raise Refused("a question is waiting in this session: answer it")
        ends = [r for r in self.log(sid).records() if r["type"] == "turn_end"]
        if not ends:
            raise Refused("this session has no turn to go on with")
        if ends[-1]["outcome"] == "answered":
            raise Refused("its last turn was answered; send a message instead")
        return self.admit(
            Message(msg_id, CONTINUE, session=sid, source=source),
            extra={"continue": True},
        )

    def start(self):
        """Finish what a crash or a stop left, then run what is queued."""
        # The parts first: jobs a crash cut off are told, so their notices are
        # repaired and run like any queued message
        for part in self.parts.values():
            part.begin()
        sessions = dict.fromkeys(row["session"] for row in self.store.unfinished())
        for sid in sessions:
            lock = self.session_lock(sid)
            try:
                lock.acquire()
            except Busy as error:
                self.blocked.add(sid)
                print(f"Skipping {sid}: {error}")
                continue
            try:
                for row in self.store.rows(sid):
                    if row["status"] != "done":
                        self.repair(row, lock)
            finally:
                lock.release()
        self.started = True
        self.dispatch()

    def repair(self, row, lock=None):
        own = lock is None
        lock = lock or self.session_lock(row["session"])
        if own:
            lock.acquire()
        try:
            self._repair(row, lock)
        finally:
            if own:
                lock.release()

    def _repair(self, row, lock):
        sid = row["session"]
        current = self.store.query("SELECT * FROM inbox WHERE id = ?", row["id"])[0]
        if current["status"] == "done":
            return
        row = current
        log = self.log(sid, lock)
        try:
            all_records = log.records()
            records = [r for r in all_records if r.get("inbox") == row["id"]]
            ends = [r for r in records if r["type"] == "turn_end"]
            outcome = ends[-1]["outcome"] if ends else None
            pending = history.pending(all_records)
        except Exception:
            upgrade.record_failure(log, row["id"], traceback.format_exc())
            self.store.finish(row["arrival"], sid, "failed")
            return
        # A steer, or a reply to an ask, already in the log
        if any((r.get("steer") or r["type"] == "answer") and r.get("inbox") == row["id"]
               for r in all_records):
            self.store.done(row["id"])
            return
        if row["kind"] == "stop":
            if not pending:
                self.store.done(row["id"])
            return
        ask = history.parked(all_records)
        if ask is not None and pending and pending[0].get("inbox") == row["id"]:
            # Parked: it waits for its reply, with no resume and no budget used
            self.store.park(sid, ask, row["id"])
            return
        if ends:
            # It ended; only the row's update was lost
            failed = outcome in FAILED
            state = "failed" if failed else "idle"
            self.store.finish(row["arrival"], sid, state)
        elif records:
            stops = self.store.stops(sid, row)
            if stops:
                try:
                    if history.version(all_records) != history.FORMAT:
                        self.upgrade_session(sid, log)
                        pending = history.pending(log.records())
                except Exception:
                    upgrade.record_failure(log, row["id"], traceback.format_exc())
                    self.store.finish(row["arrival"], sid, "failed")
                    for stop in stops:
                        self.store.done(stop["id"])
                    return
                self.repair_end(log, pending, row, "interrupted",
                                "stopped before a restart", stopped=True)
                for stop in stops:
                    self.store.done(stop["id"])
                self.store.finish(row["arrival"], sid, "idle")
            elif row["clean_stop"]:
                # A clean stop cut it off: it resumes without using a try
                self.store.resume(row["arrival"], sid, counted=False)
            elif row["resumes"] < MAX_RESUMES:
                self.store.resume(row["arrival"], sid)
            else:
                self.repair_end(log, pending, row, "failed", "cut off after 3 resumes")
                self.store.finish(row["arrival"], sid, "failed")
        elif row["status"] == "delivered":
            # Marked begun with no record of it: nothing ran, so it can run
            self.store.requeue(row["arrival"], sid)

    def repair_end(self, log, pending, row, outcome, detail, stopped=False):
        """Close a cut-off turn without calling either model."""
        if history.version(log.records()) == history.FORMAT:
            writer = self.live.get(row["session"])
            if writer is None:
                writer = Session(None, [], "", log, {})
            else:
                writer.rebuild(log)
            write = writer.write
        else:
            write = log.append
        for fields in history.closures(pending, history.UNKNOWN, "unknown"):
            write("tool", **fields)
        if stopped:
            write("note", kind="interrupted", text=INTERRUPTED)
            for steer in self.store.steers(row["session"], row["arrival"]):
                write("user", **history.steer_fields(steer))
                self.store.done(steer["inbox"])
        write("turn_end", inbox=row["id"], outcome=outcome, detail=detail,
              seconds=0.0, agent_seconds=0.0, wait_seconds=0.0)

    def dispatch(self):
        """Start a runner for each session with queued messages and none running."""
        with self.lock:
            if not self.accepting:
                return
            for sid in self.store.waiting():
                if sid not in self.runners and sid not in self.blocked:
                    thread = threading.Thread(
                        target=self.runner, args=(sid,), name=f"session-{sid}"
                    )
                    # A stop doesn't wait on a thread past the drain
                    thread.daemon = True
                    self.runners[sid] = thread
                    thread.start()

    def runner(self, sid):
        """Run a session's queued messages one at a time, then leave."""
        drained = False
        lock = self.session_lock(sid)
        try:
            lock.acquire()
        except Busy as error:
            print(f"Skipping {sid}: {error}")
            with self.changed:
                self.blocked.add(sid)
                del self.runners[sid]
                self.changed.notify_all()
            return
        try:
            drained = self.run_queued(sid, lock)
        finally:
            with self.changed:
                lock.release()
                del self.runners[sid]
                if not drained:
                    # Left to the next start's repair: its queued message no
                    # longer keeps this host up
                    self.blocked.add(sid)
                self.changed.notify_all()
        if drained:
            settled = self.settled(sid)
            if settled is not None:
                self.publish(sid, {"settled": settled})
            self.dispatch()

    def run_queued(self, sid, lock):
        while True:
            # Checking for work and leaving are one step, so a message admitted
            # meanwhile either is seen here or finds no runner and starts one
            with self.lock:
                row = self.store.next_queued(sid) if self.accepting else None
                if row is None:
                    return True
            try:
                self.run_turn(sid, row, lock=lock)
            except Exception:
                # The log or the database failed, not the turn: leave this session
                # to the next start's repair rather than loop on it
                traceback.print_exc()
                return False

    def run_turn(self, sid, row, lock=None):
        own = lock is None
        lock = lock or self.session_lock(sid)
        if own:
            lock.acquire()
        control = Control(
            steers=lambda: (
                self.store.steers(
                    sid, row["arrival"], notices_only=control.stop.is_set()
                ) if self.accepting else []
            ),
            delivered=lambda steer: self.store.done(steer["inbox"]),
            draft=lambda step, draft: self.draft(sid, step, draft),
        )
        with self.changed:
            self.running[sid] = (control, row)
        try:
            self._run_turn(sid, row, lock, control)
        finally:
            with self.changed:
                self.running.pop(sid, None)
                self.changed.notify_all()
            if own:
                lock.release()

    def _run_turn(self, sid, row, lock, control):
        log = self.log(sid, lock)
        try:
            records = log.records()
            pending = history.pending(records)
            begun = any(
                r["type"] == "user" and r.get("inbox") == row["id"] for r in records
            )
        except Exception:
            upgrade.record_failure(log, row["id"], traceback.format_exc())
            self.store.finish(row["arrival"], sid, "failed")
            return
        ask = history.parked(records)
        if ask is not None and pending[0].get("inbox") != row["id"]:
            self.take_reply(sid, row, ask, pending[0]["inbox"], log, control)
            return
        if pending and pending[0].get("inbox") != row["id"]:
            raise ValueError("the queued message is not the log's open turn")
        if begun and not pending:
            raise ValueError("no open turn to resume")
        try:
            if log.path.exists() and history.version(records) != history.FORMAT:
                self.upgrade_session(sid, log)
                records = log.records()
        except Exception:
            upgrade.record_failure(log, row["id"], traceback.format_exc())
            self.store.finish(row["arrival"], sid, "failed")
            return
        payload = json.loads(row["payload"])
        text = payload["text"]
        workspace = Path(self.store.session(sid)["workspace"])
        if not workspace.is_dir():
            self.workspace_gone(sid, row, log, records, begun, text, workspace)
            return
        try:
            session = self.harness_for_turn(sid, log, records, begun)
        except Exception:
            begun = any(
                r["type"] == "user" and r.get("inbox") == row["id"] for r in records
            )
            if begun:
                for fields in history.closures(
                    history.pending(records), history.UNKNOWN, "unknown"
                ):
                    log.append("tool", **fields)
            else:
                log.append("user", inbox=row["id"], source=row["source"], text=text)
            self.store.begin(row["arrival"], sid)
            log.append(
                "turn_end", inbox=row["id"], outcome="crashed",
                error=traceback.format_exc(),
            )
            self.store.finish(row["arrival"], sid, "failed")
            return
        records = session.log.records()
        begun = any(
            r["type"] == "user" and r.get("inbox") == row["id"] for r in records
        )
        if begun:
            turn = session.resume(inbox=row["id"], control=control)
        else:
            turn = session.open_turn(
                text, inbox=row["id"], source=row["source"], control=control,
                continued=bool(payload.get("continue")),
            )
        self.store.begin(row["arrival"], sid)
        self.finish_turn(sid, session, turn, row)

    def workspace_gone(self, sid, row, log, records, begun, text, workspace):
        """End the turn, its history kept: no command can run in a folder that
        isn't there."""
        if begun:
            for fields in history.closures(
                history.pending(records), history.UNKNOWN, "unknown"
            ):
                log.append("tool", **fields)
        else:
            log.append("user", inbox=row["id"], source=row["source"], text=text)
        self.store.begin(row["arrival"], sid)
        log.append(
            "turn_end", inbox=row["id"], outcome="failed",
            detail=f"this session's workspace {workspace} is gone",
        )
        self.store.finish(row["arrival"], sid, "failed")

    def finish_turn(self, sid, session, turn, row):
        """Run the turn, then settle its row: done, or still delivered while it
        waits on an ask."""
        try:
            outcome = session.run(turn)
        except Exception:
            ends = [r for r in session.log.records()
                    if r["type"] == "turn_end" and r.get("inbox") == row["id"]]
            if not ends:
                raise
            outcome = ends[-1]["outcome"]
        if outcome == "waiting":
            self.store.park(sid, history.parked(session.log.records()), row["id"])
            return
        state = "failed" if outcome in FAILED else "idle"
        with self.changed:
            self.store.finish(row["arrival"], sid, state)
            for stop in self.store.stops(sid, row):
                self.store.done(stop["id"])

    def take_reply(self, sid, row, ask, turn_id, log, control):
        """The parked session's next message is the reply to its ask: what it
        answers is the reply step's (a part's, permissions/host.py: the box's
        choice, or words the consent check reads), or else the choice picked
        at the box alone. A yes runs the call; anything else cancels the ask
        and joins the turn as a message. The box's No declines the call and
        ends the turn."""
        payload = json.loads(row["payload"])
        text = payload["text"]
        choice, verdict = self.reply_step(sid, ask, payload)
        yes = choice is not None and choice != NO
        session = self.harness(sid, log)
        fields = {
            "ask": ask["ask"], "yes": yes, "inbox": row["id"],
            "source": row["source"], "text": text, "consent": verdict,
        }
        if choice is not None:
            fields["choice"] = choice
        turn = session.reply(fields, control=control)
        self.store.close_ask(ask["ask"], row["id"], yes, row["source"])
        turn_row = self.store.query("SELECT * FROM inbox WHERE id = ?", turn_id)[0]
        with self.changed:
            # A stop now belongs to the parked turn, not to the reply
            self.running[sid] = (control, turn_row)
        self.finish_turn(sid, session, turn, turn_row)

    def reply_step(self, sid, ask, payload):
        """(choice, verdict) for a reply to a parked ask."""
        for part in self.parts.values():
            answer = part.reply(sid, ask, payload)
            if answer is not None:
                return answer
        return picked(ask, payload), None

    def upgrade_session(self, sid, log):
        """Ask the current harness for its header, then restore it from the upgrade."""
        info = self.store.session(sid)
        fresh = fresh_log(self.paths.sessions, info["model"])
        session = self.make_session(sid, info, log=fresh, ask=PARK)
        upgrade.upgrade_file(log, session.session_record())
        session.rebuild(log)
        self.give(sid, session)
        self.live[sid] = session

    def harness_for_turn(self, sid, log, records, begun):
        """The harness a turn runs on. A new turn takes what the user set
        since the last one (mode, model, workspace), logged as a switch
        note the model reads; a begun one (parked, or cut off by a restart)
        keeps what it started with."""
        info = self.store.session(sid)
        switch = json.loads(info["switch"] or "{}")
        logged = history.running_with(records)
        if begun:
            # Built as it started, if it must be built again
            before = {name: logged[name] for name in switch if name in logged}
            return self.harness(sid, log, info={**info, **before})
        if not switch:
            return self.harness(sid, log)
        self.live.pop(sid, None)
        # The effort has no column of its own: the switch carries it
        session = self.harness(sid, log, info={**info, **switch})
        changes = {
            name: (logged.get(name), new) for name, new in switch.items()
            if logged.get(name) != new
        }
        if changes and records:
            session.write(
                "note", kind="switch", text=switch_note(changes, session),
                **{"from": {name: old for name, (old, _) in changes.items()}},
                to={name: new for name, (_, new) in changes.items()},
                settings=session.fresh_settings,
            )
            session.settings = session.fresh_settings
        self.store.update_session(sid, switch=None)
        return session

    def harness(self, sid, log=None, info=None):
        """Build once per runtime; the harness owns its history on disk.
        `info`: the session's row, or what its log runs with (a begun turn)."""
        if sid not in self.live:
            self.live[sid] = self.make_session(
                sid, info or self.store.session(sid), log=log or self.log(sid),
                ask=PARK,
            )
            self.give(sid, self.live[sid])
        elif log is not None:
            session = self.live[sid]
            session.log.lock = log.lock
            session.log.count = None
            if session.log.records():
                session.rebuild(session.log)
        return self.live[sid]

    def give(self, sid, session):
        """Each part's share of a harness built for this session (the jobs:
        its tools start jobs through the host)."""
        for part in self.parts.values():
            part.give(sid, session)

    def stop_turn(self, sid, msg_id, source):
        """Commit the stop before setting its flag or sending the ack."""
        with self.changed:
            if not self.accepting:
                raise Refused("the host is shutting down")
            seen = self.store.query("SELECT * FROM inbox WHERE id = ?", msg_id)
            if seen:
                if seen[0]["kind"] != "stop" or seen[0]["session"] != sid:
                    raise Refused("message id already used")
                return {"stopped": True, "withdrawn": []}
            running = self.running.get(sid)
            if running is None or not history.pending(self.log(sid).records()):
                return {"stopped": False, "withdrawn": []}
            control, row = running
            self.store.admit(msg_id, sid, "stop", source, {"turn": row["id"]})
            control.stop_detail = f"stop from {source}"
            control.stop.set()
            # What the turn hasn't taken goes back to its sender's input (Pi)
            withdrawn = self.store.withdraw_messages(sid, row["arrival"])
            self.changed.notify_all()
        if withdrawn:
            self.publish(sid, {"withdrawn": withdrawn})
        return {"stopped": True, "withdrawn": withdrawn}

    def subscribe(self, sid):
        """A watcher for the session's records from now on; subscribe before
        reading the log's catch-up, so nothing falls between the two."""
        if self.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        token, watcher = object(), Watcher()
        with self.watch_lock:
            self.watchers.setdefault(sid, {})[token] = watcher
        return token, watcher

    def unsubscribe(self, sid, token):
        with self.watch_lock:
            watchers = self.watchers.get(sid, {})
            watchers.pop(token, None)
            if not watchers:
                self.watchers.pop(sid, None)

    def publish(self, sid, event):
        with self.watch_lock:
            watchers = list(self.watchers.get(sid, {}).values())
        for watcher in watchers:
            watcher.put(event)

    def close_watchers(self, reason):
        """Every watch ends with a `bye` and its reason."""
        with self.watch_lock:
            watchers = [w for ws in self.watchers.values() for w in ws.values()]
        for watcher in watchers:
            watcher.close(reason)

    def settled(self, sid):
        """None while a turn runs or a queued message would start one; else
        what is left: the open ask's question, if any, and what the parts
        leave (running jobs) (Pi's `agent_settled`)."""
        with self.lock:
            if sid in self.runners or sid in self.store.waiting():
                return None
        ask = self.store.open_ask(sid)
        left = {
            "ask": None if ask is None else {
                "id": ask["id"], "tool": ask["tool"], "command": ask["command"],
                "why": ask["why"],
            },
        }
        for part in self.parts.values():
            left.update(part.settled(sid))
        return left

    def append_between_turns(self, sid, refused, type, **fields):
        """Append a record to a session's log while no turn runs or waits for
        an answer (a grant taken back, permissions/host.py); else Refused,
        with `refused`."""
        with self.lock:
            row = self.store.session(sid)
            if (sid in self.runners or sid in self.store.waiting()
                    or row["state"] in ("running", "waiting")):
                raise Refused(refused)
            lock = self.session_lock(sid)
            try:
                lock.acquire()
            except Busy as error:
                raise Refused(str(error)) from None
            try:
                self.log(sid, lock).append(type, **fields)
            finally:
                lock.release()

    def busy(self):
        """What keeps the host up besides its clients: turns running or
        resuming, messages that can start one, running jobs. A parked ask waits
        on disk and keeps nothing up."""
        with self.lock:
            turns = sorted(self.runners)
            queued = [s for s in self.store.waiting() if s not in self.blocked]
        reasons = {"turns": turns, "queued": sorted(set(queued) - set(turns))}
        for part in self.parts.values():
            reasons.update(part.busy())
        return {name: value for name, value in reasons.items() if value}

    def wait(self, timeout=None):
        """True once no turn is running and nothing is queued (or stopping)."""

        def idle():
            waiting = set(self.store.waiting()) - self.blocked
            return not self.runners and (not self.accepting or not waiting)

        with self.changed:
            return self.changed.wait_for(idle, timeout)

    def close_if_idle(self):
        """Take no new messages, but only if nothing is busy, in one step under
        the lock that starting a turn takes: True if the host may go."""
        with self.lock:
            queued = [s for s in self.store.waiting() if s not in self.blocked]
            parts = any(v for part in self.parts.values()
                        for v in part.busy().values())
            if self.runners or queued or parts:
                return False
            self.accepting = False
            return True

    def stop(self, timeout=10):
        """Take no new messages; give running turns up to `timeout` to end.

        True if they did. A turn still running resumes at the next start,
        without using a try: this is a clean stop, not a crash.
        """
        with self.lock:
            self.accepting = False
        with self.changed:
            drained = self.changed.wait_for(lambda: not self.runners, timeout)
            for _, row in self.running.values():
                self.store.mark_clean_stop(row["id"])
        # Then the parts: each job's loss is written before it is killed
        for part in self.parts.values():
            part.shutdown()
        return drained
