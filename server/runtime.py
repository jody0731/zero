"""The runtime as a library: messages in, turns run, records out.

A message is admitted into the inbox before its sender hears back. Each session
with queued messages gets a runner thread that runs its turns one at a time,
oldest first; different sessions run side by side. The harness writes every
turn's history to the session log. The server's server (server/server.py) is a
thin shell around this; tests drive it directly.

The order of writes, so a crash anywhere leaves something the boot repair can
finish: admit (the inbox row) → ack; the `user` record → the row delivered and
the session running; the `turn_end` record → the row done and the session idle
or failed. A turn that parks on an ask: the `ask` record → its `asks` row
and the session waiting, the turn's row still delivered; the session's next
message is the reply: the `answer` record → the ask closed and the reply's row
done. A background job's end, check-in or loss is a `job` message written with
its row (jobs/service.py): an idle session starts a turn on it, a running
turn takes it in as a steer, a parked one keeps it until the ask is answered.

Each session has a workspace, a folder fixed when the session is made; none
means `/`, the whole machine. Every record a session's log appends is
handed to the session's watchers in memory, so a watch reads the log only once,
for its catch-up. busy_with() says what keeps the server up; a parked ask doesn't.
A runner past its last turn still has its last word to say (`settled`, then a
dispatch that starts a turn on a message admitted meanwhile), so the runtime
isn't at rest until it has: `wait` and the idle close count it (`leaving`).
A clean stop marks the turns it cuts off, so they resume without using a try:
only a crash uses one (Pi's "a close is not a crash").

From Pi: a message sent while a turn runs either steers it or
waits as a follow-up until the turn answers, and `admit` says which became of
it; a stop gives the turn's untaken steers and follow-ups back to their sender;
`continue` takes a turn that ended without an answer on with a harness note;
`settled` tells a session's watchers that nothing more will happen on its own.
The server keeps what the user has seen of each session, and the list's
listeners hear each change to it (session_actions.py).

The runtime names no component: the assembly hands in how a session is
made, the modes, and the services (server/services.py), which add their
tables, messages, reply step and share of each session.

Parallel by design (7.5.2): each session owns its state, behind its own lock
(server/session_state.py): admission, its runner's next message, its turn's
end, a stop. A message to one session never waits on another's. The runtime
keeps the table of them behind one door (`door`), held only for a lookup or
a count (what runs, what is leaving, what is being admitted), never for a
query, a commit, a thread's start or a service; a session's lock is taken
before the door, never after. Dispatch starts the runner of the session a
message came to, not a look through every session's inbox.
"""

import json
import sqlite3
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import settings
from core import history
from core.control import Control
from core.loop import INTERRUPTED, Session
from core.asking import NO, PARK, YES
from sessions import upgrade
from sessions.log import Log, fresh_log
from sessions.lock import Busy, SessionLock
from server import rebuild
from server.rebuild import FAILED
from server.services import Refused
from server.session_actions import (
    ID_TRIES, SessionActions, asked, check_thinking_effort, new_id, title,
)
from server.session_state import SessionState
from server.store import Store, UnknownSession

MAX_RESUMES = 3
WHEN_BUSY = ("steer", "queue")
# What a turn begun by `continue` opens with: the user's wish, not their words
CONTINUE = "[The user asked you to go on with the task.]"
# Events a watch may have waiting; a watcher further behind is let go with
# a `bye` "behind", and follows again from the log, which holds them all
WATCH_QUEUE_MAX = 2000
# Why a watch the runtime lets go ends (protocol/messages.py): too far
# behind, or its session deleted
BEHIND = "behind"
DELETED = "deleted"
# How often `wait` looks again at a message no runner has taken yet
WAIT_LOOK_SECONDS = 0.05


@dataclass
class Message:
    """A message from a client. `session` None starts a new session."""

    id: str
    text: str
    session: str | None = None
    mode: str | None = None
    model: str | None = None
    # A new session's thinking effort; None: config.toml's
    thinking_effort: str | None = None
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
    what changed, and what the hooks say it runs with now (the `switched`
    hook: Environment's workspace and sandbox)."""
    said = "; ".join(f"{name} {old} → {new}" for name, (old, new) in changes.items())
    now = session.hooks.collect("switched", session)
    return f"[Harness note: the user changed this session's {said}. From here on:{now}]"


class Watcher:
    """One watching connection's queue: records and events, in order, at
    most `most` of them (design rule 12: every queue has a cap)."""

    def __init__(self, most=None):
        self.events = deque()
        self.most = WATCH_QUEUE_MAX if most is None else most
        self.ready = threading.Condition()
        # Why the server closes this watch: a `bye` reason, once it does
        self.bye = None

    def put(self, event):
        with self.ready:
            if self.bye is not None:
                return
            # A draft holds the whole reply so far, so a newer one replaces
            # one still queued: a slow reader gets the latest, not each
            if "draft" in event and self.events and "draft" in self.events[-1]:
                self.events[-1] = event
            elif len(self.events) >= self.most:
                # Too far behind: let go, to follow again from the log
                self.events.clear()
                self.bye = BEHIND
            else:
                self.events.append(event)
            self.ready.notify_all()

    def close(self, reason):
        with self.ready:
            self.bye = self.bye or reason
            self.ready.notify_all()

    def take(self, timeout):
        """What came since the last take, waiting up to `timeout` for any."""
        with self.ready:
            self.ready.wait_for(lambda: self.events or self.bye, timeout)
            events = list(self.events)
            self.events.clear()
            return events


class Runtime(SessionActions):
    def __init__(self, paths, make_session, modes, services=(), model=None,
                 mode=None, config=None, default_model=None):
        """`make_session(sid, info, log=None, ask=PARK)` builds a harness. An
        upgrade supplies a fresh log to obtain the current header without writes.
        `modes`: every mode's name, least to most permissive; a session's asks
        go to the clients following it (they park). `services`: the components'
        (server/services.py), attached once the store is open.
        `config()`: config.toml's settings as they stand (settings.Config), read
        for each new session: its mode and model unless the message names them.
        `model` and `mode` win over the config's. `default_model`: the model
        where neither the message nor the config names one."""
        paths.make()
        self.paths = paths
        self.modes = tuple(modes)
        if mode and mode not in self.modes:
            raise ValueError(f"unknown mode {mode!r}; modes: {self.modes}")
        self.store = Store(
            paths.db, [service.schema for service in services if service.schema],
            [service.renamed_columns for service in services],
            [service.added_columns for service in services],
        )
        self.config = config or (lambda: settings.DEFAULTS)
        self.make_session = make_session
        self._model = model
        self.default_model = default_model
        self._mode = mode
        # Each session's own state while the server runs: id → SessionState
        self.states = {}
        # How a session's lock is made (a probe times them)
        self.make_lock = threading.Lock
        # The door to the table and the counts below; `activity` hears each
        # count change
        self.door = threading.Lock()
        self.activity = threading.Condition(self.door)
        # Sessions held by a runner now, and threads of theirs past their
        # last turn still to say their last word (settled, then a dispatch
        # that may start a turn on a message admitted meanwhile): nothing is
        # at rest until both are none
        self.runner_count = 0
        self.leaving_count = 0
        # Admissions under way, and how many runners and admissions there
        # ever were: a look at what is busy made in two steps sees one that
        # came between (close_if_idle)
        self.admitting = 0
        self.runners_held = 0
        self.admissions = 0
        self.accepting = True
        self.started = False
        # The session list's listeners wait for its version to move
        self.list_version = 0
        self.list_changed = threading.Condition()
        self.store.on_change = self.notify_list_changed
        # The components' services by name, the messages they add, and those of
        # them that only look
        self.services = {}
        self.messages = {}
        self.looking_messages = ()
        self.workflow_messages = ()
        for service in services:
            service.attach(self)
            self.services[service.name] = service
            self.messages.update(service.messages())
            self.looking_messages += tuple(service.looking_messages)
            self.workflow_messages += tuple(service.workflow_messages)

    @property
    def model(self):
        """A new session's model, where its first message names none."""
        return self._model or self.config()["model"] or self.default_model

    @property
    def mode(self):
        return self._mode or self.config()["mode"]

    def check_mode(self, mode):
        """The mode, if the server has it; Refused if not."""
        if mode not in self.modes:
            raise Refused(f"unknown mode {mode!r}; modes: {self.modes}")
        return mode

    def state(self, sid):
        """The session's own state, made on its first use."""
        found = self.states.get(sid)
        if found is not None:
            return found
        with self.door:
            found = self.states.get(sid)
            if found is None:
                found = self.states[sid] = SessionState(self.make_lock())
            return found

    def all_states(self):
        """(id, state) for every session the server keeps, as they are now."""
        with self.door:
            return list(self.states.items())

    # What runs and what is kept, as one look across the sessions: for the
    # tests, the evals and the probes; the runtime reads each session's own

    @property
    def runners(self):
        return {sid: s.runner for sid, s in self.all_states() if s.runner is not None}

    @property
    def running(self):
        return {sid: s.running for sid, s in self.all_states()
                if s.running is not None}

    @property
    def live(self):
        return {sid: s.harness for sid, s in self.all_states()
                if s.harness is not None}

    @property
    def watchers(self):
        return {sid: dict(s.watchers) for sid, s in self.all_states() if s.watchers}

    @property
    def blocked(self):
        return {sid for sid, s in self.all_states() if s.blocked}

    @property
    def leaving(self):
        return {sid: s.leaving for sid, s in self.all_states() if s.leaving}

    def hold_runner(self, state, thread, while_accepting=False):
        """`state.lock` held: `thread` is the session's runner, so no other
        turn of it starts; counted, so a look at what is busy made in two
        steps sees one that came between (close_if_idle). `while_accepting`:
        not once the idle close has begun (False then)."""
        with self.activity:
            if while_accepting and not self.accepting:
                return False
            self.runner_count += 1
            self.runners_held += 1
        state.runner = thread
        return True

    def let_go(self, state, leaving):
        """`state.lock` held: its runner lets go of the session; `leaving`,
        with its last word still to say (said_last_word)."""
        state.runner = None
        if leaving:
            state.leaving += 1
        with self.activity:
            self.runner_count -= 1
            if leaving:
                self.leaving_count += 1
            self.activity.notify_all()

    def said_last_word(self, state):
        with state.lock:
            state.leaving -= 1
        with self.activity:
            self.leaving_count -= 1
            self.activity.notify_all()

    def busy_now(self):
        """`door` held: a runner holds a session, says its last word, or a
        message is being admitted."""
        return bool(self.runner_count or self.leaving_count or self.admitting)

    def notify_list_changed(self):
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
            self.store.mark_ended(sid, record["seq"])
        # Any record ends the draft: the reply's own, a retry, a stop's note
        self.state(sid).draft = None
        self.publish(sid, {"record": record})
        self.meter(sid, record)

    def meter(self, sid, record):
        """The context's meter after a record, sent to the watchers when it
        changes: from the whole log the first time this server sees the
        session or when the line's setting has changed since, then record by
        record (core/history.py)."""
        compact_at = self.meter_line()
        state = self.state(sid)
        before = state.meter
        if before is None or state.meter_line != compact_at:
            now = None
            for each in self.log(sid).branch():
                now = history.metered(now, each, compact_at)
        else:
            now = history.metered(before, record, compact_at)
        state.meter = now
        state.meter_line = compact_at
        if now != before and now["tokens"]:
            self.publish(sid, {"context": now})

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
        self.state(sid).draft = event
        self.publish(sid, event)

    def current_draft(self, sid):
        """The draft event of the reply streaming in now, or None."""
        state = self.states.get(sid)
        return None if state is None else state.draft

    def session_lock(self, sid):
        return SessionLock(self.paths.locks / f"{sid}.lock")

    def meter_line(self):
        """The line the user's context meter shows, as the services say it
        (the context component's setting); None: the model's window."""
        for service in self.services.values():
            line = service.meter_line()
            if line is not None:
                return line
        return None

    def message_fields(self):
        """What a message carries beside its text onto its user record:
        core's (history.MESSAGE_FIELDS) and those the services read."""
        names = list(history.MESSAGE_FIELDS)
        for service in self.services.values():
            names += [n for n in service.message_fields if n not in names]
        return names

    def admit(self, message, extra=None):
        """Commit a message to the inbox, then return the ack: its session and
        its handled_as, what became of it. `extra`: payload fields."""
        if not self.accepting:
            raise Refused("the server is shutting down; send it again once it's back")
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
            mode = self.check_mode(message.mode or self.mode)
            model = message.model or self.model
            if not model:
                raise Refused(
                    "no model is set yet: zero, the terminal, connects one "
                    "(Connect a model; later /settings, Models and keys), or "
                    "--model names one"
                )
            new = {
                "title": title(message.text),
                "source": message.source,
                "model": model,
                "mode": mode,
                "workspace": str(Path(workspace).resolve()),
            }
            if message.thinking_effort is not None:
                # Its first turn builds it with the effort, as a switch would
                effort = check_thinking_effort(
                    "thinking_effort", message.thinking_effort
                )
                new["switch"] = json.dumps({"thinking_effort": effort})
        payload = {"text": message.text, **(extra or {})}
        if message.reply_to is not None:
            payload["reply_to"] = message.reply_to
            if message.choice is not None:
                payload["choice"] = message.choice
        with self.door:
            if not self.accepting:
                raise Refused("the server is shutting down; send it again once "
                              "it's back")
            self.admitting += 1
            self.admissions += 1
        try:
            admitted, sid, handled_as = self.admit_to_session(
                message, sid, new, payload)
            if admitted and self.started:
                self.dispatch(sid)
        finally:
            with self.activity:
                self.admitting -= 1
                self.activity.notify_all()
        return {"admitted": admitted, "session": sid, "handled_as": handled_as}

    def admit_to_session(self, message, sid, new, payload):
        """(admitted, session id, handled_as): what becomes of the message
        decided and its row committed under the session's own lock. A new
        session takes a new id, and another when one is taken."""
        for tries in range(1, ID_TRIES + 1):
            if new:
                sid = new_id()
            state = self.state(sid)
            with state.lock:
                handled_as = "started" if new else self.handled_as(sid, state, message)
                said = dict(payload)
                if handled_as == "queued":
                    said["follow_up"] = True
                try:
                    admitted, sid = self.store.admit(
                        message.id, sid, "user", message.source, said,
                        new_session=new,
                    )
                except UnknownSession:
                    raise Refused(f"no session {sid!r}") from None
                except sqlite3.IntegrityError:
                    if not new or tries == ID_TRIES:
                        raise
                    continue
                return admitted, sid, handled_as

    def handled_as(self, sid, state, message):
        """What becomes of a message to this session, decided under its
        lock: `reply` to its open ask, `steered` or `queued` (a follow-up)
        while a turn runs or waits to, `started` otherwise."""
        info = self.store.session(sid)
        if info is None:
            raise Refused(f"no session {sid!r}")
        if info["state"] == "waiting" and self.store.open_ask(sid) is not None:
            return "reply"
        busy = state.runner is not None or self.store.has_queued(sid)
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
        state = self.state(sid)
        with state.lock:
            if state.runner is not None or self.store.has_queued(sid):
                raise Refused("a turn is running in this session; nothing to continue")
            if self.store.open_ask(sid) is not None:
                raise Refused("a question is waiting in this session: answer it")
        ends = [r for r in self.log(sid).branch() if r["type"] == "turn_end"]
        if not ends:
            raise Refused("this session has no turn to go on with")
        if ends[-1]["outcome"] == "answered":
            raise Refused("its last turn was answered; send a message instead")
        return self.admit(
            Message(msg_id, CONTINUE, session=sid, source=source),
            extra={"continue": True},
        )

    def start(self):
        """Bring the database in step with the logs, finish what a crash or a
        stop left, then run what is queued."""
        self.rebuild_ahead()
        # The services next: jobs a crash cut off are told, so their notices
        # are repaired and run like any queued message
        for service in self.services.values():
            service.begin()
        sessions = dict.fromkeys(row["session"] for row in self.store.unfinished_rows())
        for sid in sessions:
            lock = self.session_lock(sid)
            try:
                lock.acquire()
            except Busy as error:
                self.state(sid).blocked = True
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

    def adopt_logs(self):
        """Design rule 9: a log with no row (its database lost or replaced,
        a fork cut off between its log and its row, a log copied in) is
        listed again, its row made from its header (rebuild.adopted_row),
        its other rows then rebuilt from it as any log ahead of its rows
        is. A file that isn't a session's log is left alone."""
        known = self.store.session_ids()
        for path in sorted(self.paths.sessions.glob("*.jsonl")):
            sid = path.stem
            # An upgraded log's original beside it starts as a log does
            if sid in known or upgrade.is_backup(path):
                continue
            try:
                records = self.log(sid).records()
                if not records or records[0].get("type") != "session":
                    # Not a session's log
                    continue
                records = upgrade.migrate(records, time=upgrade.timestamp())
                row = rebuild.adopted_row(sid, records, title)
                if row is None:
                    continue
                self.store.add_session(sid, row)
                print(f"ZERO's server: listed {sid} again from its log "
                      "(it had no row)", flush=True)
            except Exception:
                traceback.print_exc()

    def rebuild_ahead(self):
        """Design rule 9: rebuild the rows of each session whose log is
        ahead of them (past its row's `log_seq`), from the log, under the
        session's lock: the kernel's (server/rebuild.py) and each service's,
        in one transaction. A session in step is only looked at: its log's
        last record, read from the end. A log with no row is adopted first."""
        self.adopt_logs()
        services = [service.rebuild for service in self.services.values()]
        for row in self.store.sessions():
            sid = row["id"]
            log = self.log(sid)
            try:
                last = log.last_record()
            except ValueError:
                # A broken log: its next turn records the failure
                traceback.print_exc()
                continue
            if last is None or last.get("seq", 0) <= row["log_seq"]:
                continue
            lock = self.session_lock(sid)
            try:
                lock.acquire()
            except Busy as error:
                self.state(sid).blocked = True
                print(f"Skipping {sid}: {error}")
                continue
            try:
                records = log.records()
                # A newer ZERO's log: its rows stay as that ZERO left them
                if history.log_format(records) <= history.LOG_FORMAT:
                    records = upgrade.migrate(records, time=upgrade.timestamp())
                    self.store.rebuild(sid, rebuild.kernel_rows(records), records,
                                       services)
            except Exception:
                traceback.print_exc()
            finally:
                lock.release()

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
            all_records = log.branch()
            records = [r for r in all_records if r.get("inbox") == row["id"]]
            ends = [r for r in records if r["type"] == "turn_end"]
            outcome = ends[-1]["outcome"] if ends else None
            pending = history.unfinished_turn(all_records)
        except Exception:
            upgrade.record_failure(log, row["id"], traceback.format_exc())
            self.store.finish(row["arrival"], sid, "failed")
            return
        # A reply to an ask already in the log: the ask closed with it, if the
        # server died before its row was
        answers = [r for r in all_records
                   if r["type"] == "answer" and r.get("inbox") == row["id"]]
        open_ask = self.store.open_ask(sid) if answers else None
        if open_ask is not None and open_ask["id"] == answers[-1].get("ask"):
            self.store.close_ask(open_ask["id"], row["id"],
                                 bool(answers[-1].get("yes")), row["source"])
            return
        # A steer, or a reply to an ask, already in the log
        if answers or any(r.get("steer") and r.get("inbox") == row["id"]
                          for r in all_records):
            self.store.mark_steer_done(row["id"])
            return
        if row["kind"] == "stop":
            if not pending:
                self.store.mark_steer_done(row["id"])
            return
        ask = history.parked_ask(all_records)
        if ask is not None and pending and pending[0].get("inbox") == row["id"]:
            # Parked: it waits for its reply, with no resume and no budget used
            self.store.park(sid, ask, row["id"])
            return
        if ends:
            # It ended; only the row's update was lost (and the services'
            # word of it, which they keep once)
            failed = outcome in FAILED
            state = "failed" if failed else "idle"
            self.store.finish(row["arrival"], sid, state)
            self.turn_ended(sid, outcome)
        elif records:
            stops = self.store.stops(sid, row)
            if stops:
                try:
                    if history.log_format(all_records) != history.LOG_FORMAT:
                        self.upgrade_session(sid, log)
                        pending = history.unfinished_turn(log.branch())
                except Exception:
                    upgrade.record_failure(log, row["id"], traceback.format_exc())
                    self.store.finish(row["arrival"], sid, "failed")
                    for stop in stops:
                        self.store.mark_steer_done(stop["id"])
                    return
                self.repair_end(log, pending, row, "interrupted",
                                "stopped before a restart", stopped=True)
                for stop in stops:
                    self.store.mark_steer_done(stop["id"])
                self.store.finish(row["arrival"], sid, "idle")
                self.turn_ended(sid, "interrupted")
            elif row["clean_stop"]:
                # A clean stop cut it off: it resumes without using a try
                self.store.resume(row["arrival"], sid, counted=False)
            elif row["resume_count"] < MAX_RESUMES:
                self.store.resume(row["arrival"], sid)
            else:
                self.repair_end(log, pending, row, "failed", "cut off after 3 resumes")
                self.store.finish(row["arrival"], sid, "failed")
                self.turn_ended(sid, "failed")
        elif row["status"] == "delivered":
            # Marked begun with no record of it: nothing ran, so it can run
            self.store.requeue(row["arrival"], sid)

    def repair_end(self, log, pending, row, outcome, detail, stopped=False):
        """Close a cut-off turn without calling either model."""
        if history.log_format(log.branch()) == history.LOG_FORMAT:
            writer = self.state(row["session"]).harness
            if writer is None:
                writer = Session(None, [], "", log, {})
            else:
                writer.restore(log)
            write = writer.write
        else:
            write = log.append
        for fields in history.closing_results(pending, history.UNKNOWN, "unknown"):
            write("tool", **fields)
        if stopped:
            write("note", kind="interrupted", text=INTERRUPTED)
            for steer in self.store.steers(row["session"], row["arrival"]):
                write("user", **history.steer_fields(steer))
                self.store.mark_steer_done(steer["inbox"])
        write("turn_end", inbox=row["id"], outcome=outcome, detail=detail,
              seconds=0.0, agent_seconds=0.0, wait_seconds=0.0)

    def dispatch(self, sid=None):
        """Start a runner for the session `sid`, if a message of it can
        start a turn and none runs; for every such session when `sid` is
        None (the start, a service's notice)."""
        if not self.accepting:
            return
        for each in ([sid] if sid is not None else self.store.waiting_sessions()):
            self.start_runner(each)

    def start_runner(self, sid):
        # Looked at first, so a session gone (deleted) gets no state again
        if not self.store.has_queued(sid):
            return
        state = self.state(sid)
        with state.lock:
            if (state.runner is not None or state.blocked
                    or not self.store.has_queued(sid)):
                return
            thread = threading.Thread(
                target=self.runner, args=(sid, state), name=f"session-{sid}",
                # A stop doesn't wait on a thread past the drain
                daemon=True,
            )
            # The idle close come first: the message waits for the next start
            if self.hold_runner(state, thread, while_accepting=True):
                thread.start()

    def runner(self, sid, state):
        """Run a session's queued messages one at a time, then leave."""
        drained = False
        lock = self.session_lock(sid)
        try:
            lock.acquire()
        except Busy as error:
            print(f"Skipping {sid}: {error}")
            with state.lock:
                state.blocked = True
                self.let_go(state, leaving=False)
            return
        try:
            drained = self.run_queued(sid, state, lock)
        finally:
            with state.lock:
                lock.release()
                if not drained:
                    # Left to the next start's repair: its queued message no
                    # longer keeps this server up
                    state.blocked = True
                # Past its last turn, the runner still has its last word to
                # say: `settled`, then a dispatch that starts a turn on a
                # message admitted meanwhile
                self.let_go(state, leaving=True)
        try:
            if drained:
                settled = self.settled(sid)
                if settled is not None:
                    self.publish(sid, {"settled": settled})
                self.dispatch(sid)
        finally:
            self.said_last_word(state)

    def run_queued(self, sid, state, lock):
        while True:
            # Checking for work and leaving are one step, so a message admitted
            # meanwhile either is seen here or finds no runner and starts one
            with state.lock:
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
            delivered=lambda steer: self.store.mark_steer_done(steer["inbox"]),
            draft=lambda step, draft: self.draft(sid, step, draft),
        )
        state = self.state(sid)
        with state.lock:
            state.running = (control, row)
        try:
            self._run_turn(sid, row, lock, control)
        finally:
            with state.lock:
                state.running = None
            if own:
                lock.release()

    def _run_turn(self, sid, row, lock, control):
        log = self.log(sid, lock)
        try:
            records = log.branch()
            pending = history.unfinished_turn(records)
            begun = any(
                r["type"] == "user" and r.get("inbox") == row["id"] for r in records
            )
        except Exception:
            upgrade.record_failure(log, row["id"], traceback.format_exc())
            self.store.finish(row["arrival"], sid, "failed")
            return
        ask = history.parked_ask(records)
        if ask is not None and pending[0].get("inbox") != row["id"]:
            self.take_reply(sid, row, ask, pending[0]["inbox"], log, control)
            return
        if pending and pending[0].get("inbox") != row["id"]:
            raise ValueError("the queued message is not the log's open turn")
        if begun and not pending:
            raise ValueError("no open turn to resume")
        try:
            if log.path.exists() and history.log_format(records) != history.LOG_FORMAT:
                self.upgrade_session(sid, log)
                records = log.branch()
        except Exception:
            upgrade.record_failure(log, row["id"], traceback.format_exc())
            self.store.finish(row["arrival"], sid, "failed")
            return
        payload = json.loads(row["payload"])
        text = payload["text"]
        workspace = Path(self.store.session(sid)["workspace"])
        if not workspace.is_dir():
            self.end_for_missing_workspace(
                sid, row, log, records, begun, text, workspace
            )
            return
        try:
            session = self.harness_for_turn(sid, log, records, begun)
        except Exception:
            begun = any(
                r["type"] == "user" and r.get("inbox") == row["id"] for r in records
            )
            if begun:
                for fields in history.closing_results(
                    history.unfinished_turn(records), history.UNKNOWN, "unknown"
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
            self.turn_ended(sid, "crashed")
            return
        records = session.log.branch()
        begun = any(
            r["type"] == "user" and r.get("inbox") == row["id"] for r in records
        )
        if begun:
            turn = session.resume(inbox=row["id"], control=control)
        else:
            turn = session.open_turn(
                text, inbox=row["id"], source=row["source"], control=control,
                continued=bool(payload.get("continue")),
                carried={name: payload[name] for name in self.message_fields()
                         if payload.get(name) is not None},
            )
        self.store.begin(row["arrival"], sid)
        self.finish_turn(sid, session, turn, row)

    def end_for_missing_workspace(self, sid, row, log, records, begun, text, workspace):
        """End the turn, its history kept: no command can run in a folder that
        isn't there."""
        if begun:
            for fields in history.closing_results(
                history.unfinished_turn(records), history.UNKNOWN, "unknown"
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
        self.turn_ended(sid, "failed")

    def finish_turn(self, sid, session, turn, row):
        """Run the turn, then settle its row: done, or still delivered while it
        waits on an ask."""
        try:
            outcome = session.run(turn)
        except Exception:
            ends = [r for r in session.log.branch()
                    if r["type"] == "turn_end" and r.get("inbox") == row["id"]]
            if not ends:
                raise
            outcome = ends[-1]["outcome"]
        if outcome == "waiting":
            self.store.park(sid, history.parked_ask(session.log.branch()), row["id"])
            self.turn_ended(sid, outcome)
            return
        ended = "failed" if outcome in FAILED else "idle"
        with self.state(sid).lock:
            # Its stops done with it; its rows in step up to its turn_end
            self.store.finish(row["arrival"], sid, ended, session.log.last_seq)
        self.turn_ended(sid, outcome)

    def take_reply(self, sid, row, ask, turn_id, log, control):
        """The parked session's next message is the reply to its ask: what it
        answers is the reply step's (a service's, permissions/service.py: the box's
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
        state = self.state(sid)
        with state.lock:
            # A stop now belongs to the parked turn, not to the reply
            state.running = (control, turn_row)
        self.finish_turn(sid, session, turn, turn_row)

    def reply_step(self, sid, ask, payload):
        """(choice, verdict) for a reply to a parked ask."""
        for service in self.services.values():
            answer = service.reply(sid, ask, payload)
            if answer is not None:
                return answer
        return picked(ask, payload), None

    def upgrade_session(self, sid, log):
        """Ask the current harness for its header, then restore it from the upgrade."""
        info = self.store.session(sid)
        fresh = fresh_log(self.paths.sessions, info["model"])
        session = self.make_session(sid, info, log=fresh, ask=PARK)
        session.header_fields = {"row": rebuild.header_row(info)}
        upgrade.upgrade_file(log, session.session_record())
        session.restore(log)
        self.session_built(sid, session)
        self.state(sid).harness = session

    def harness_for_turn(self, sid, log, records, begun):
        """The harness a turn runs on. A new turn takes what the user set
        since the last one (mode, model, workspace, effort, the judge),
        logged as a switch, a note the model reads when it names what the
        agent knows of itself; a begun one (parked, or cut off by a restart)
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
        self.state(sid).harness = None
        # The effort and the judge have no column of their own: the switch
        # carries them
        session = self.harness(sid, log, info={**info, **switch})
        changes = {
            name: (logged.get(name), new) for name, new in switch.items()
            if logged.get(name) != new
        }
        if changes and records:
            moved = {
                "from": {name: old for name, (old, _) in changes.items()},
                "to": {name: new for name, (_, new) in changes.items()},
                "settings": session.fresh_settings,
            }
            seen = {k: v for k, v in changes.items() if k in history.MODEL_VISIBLE}
            if "model" in changes:
                # A new model loses the cache anyway: the system message is
                # written again for it, which names it
                session.rebuild_system("model")
            if seen:
                # The model reads what changed of what it knows of itself
                session.write("note", kind="switch",
                              text=switch_note(seen, session), **moved)
            else:
                # The judge alone: nothing the agent knows changed
                session.write("switch", **moved)
            session.settings = session.fresh_settings
        self.store.update_session(sid, switch=None)
        return session

    def harness(self, sid, log=None, info=None):
        """Build once per runtime; the harness owns its history on disk.
        `info`: the session's row, or what its log runs with (a begun turn)."""
        state = self.state(sid)
        if state.harness is None:
            info = info or self.store.session(sid)
            session = self.make_session(sid, info, log=log or self.log(sid),
                                        ask=PARK)
            # Its header, when its first turn writes it, carries its row
            session.header_fields = {"row": rebuild.header_row(info)}
            state.harness = session
            self.session_built(sid, session)
        elif log is not None:
            session = state.harness
            session.log.lock = log.lock
            session.log.last_seq = None
            if session.log.branch():
                session.restore(session.log)
        return state.harness

    def session_built(self, sid, session):
        """Each service's share of a harness built for this session (the jobs:
        its tools start jobs through the server)."""
        for service in self.services.values():
            service.session_built(sid, session)

    def turn_ended(self, sid, outcome):
        """Each service told that a turn of the session ended or parked (a
        subagent's report to its parent). A service that fails here leaves
        the turn as it ended."""
        for service in self.services.values():
            try:
                service.turn_ended(sid, outcome)
            except Exception:
                traceback.print_exc()

    def turn_busy(self, sid):
        """Whether a turn of the session runs, or a queued message would
        start one."""
        state = self.state(sid)
        with state.lock:
            return state.runner is not None or self.store.has_queued(sid)

    def stop_turn(self, sid, msg_id, source):
        """Commit the stop before setting its flag or sending the ack."""
        state = self.state(sid)
        with state.lock:
            if not self.accepting:
                raise Refused("the server is shutting down")
            seen = self.store.query("SELECT * FROM inbox WHERE id = ?", msg_id)
            if seen:
                if seen[0]["kind"] != "stop" or seen[0]["session"] != sid:
                    raise Refused("message id already used")
                return {"stopped": True, "withdrawn": []}
            running = state.running
            if running is None or not history.unfinished_turn(self.log(sid).branch()):
                return {"stopped": False, "withdrawn": []}
            control, row = running
            self.store.admit(msg_id, sid, "stop", source, {"turn": row["id"]})
            control.stop_detail = f"stop from {source}"
            control.stop.set()
            # What the turn hasn't taken goes back to its sender's input (Pi)
            withdrawn = self.store.withdraw_messages(sid, row["arrival"])
        if withdrawn:
            self.publish(sid, {"withdrawn": withdrawn})
        return {"stopped": True, "withdrawn": withdrawn}

    def subscribe(self, sid):
        """A watcher for the session's records from now on; subscribe before
        reading the log's catch-up, so nothing falls between the two."""
        if self.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        token, watcher = object(), Watcher()
        state = self.state(sid)
        with state.lock:
            state.watchers = {**state.watchers, token: watcher}
        return token, watcher

    def unsubscribe(self, sid, token):
        state = self.states.get(sid)
        if state is None:
            return
        with state.lock:
            state.watchers = {k: w for k, w in state.watchers.items() if k is not token}

    def publish(self, sid, event):
        state = self.states.get(sid)
        if state is None:
            return
        for watcher in state.watchers.values():
            watcher.put(event)

    def close_watchers(self, reason):
        """Every watch ends with a `bye` and its reason."""
        for _, state in self.all_states():
            for watcher in state.watchers.values():
                watcher.close(reason)

    def forget_state(self, sid):
        """A deleted session's state let go of: its watches end with a `bye`
        "deleted", its harness and draft go with it."""
        with self.door:
            state = self.states.pop(sid, None)
        if state is None:
            return
        for watcher in state.watchers.values():
            watcher.close(DELETED)

    def command_session(self, unix_session):
        """The session whose command leads the Unix session `unix_session`,
        as the service that started the command knows it; None for none."""
        if unix_session is None:
            return None
        for service in self.services.values():
            found = service.command_session(unix_session)
            if found is not None:
                return found
        return None

    def settled(self, sid):
        """None while a turn runs or a queued message would start one; else
        what is left: the open ask's question, if any, and what the services
        leave (running jobs) (Pi's `agent_settled`)."""
        state = self.state(sid)
        with state.lock:
            if state.runner is not None or self.store.has_queued(sid):
                return None
        ask = self.store.open_ask(sid)
        left = {"ask": None if ask is None else asked(ask)}
        for service in self.services.values():
            left.update(service.settled(sid))
        return left

    def append_between_turns(self, sid, refused, type, **fields):
        """Append a record to a session's log while no turn runs or waits for
        an answer (a grant taken back, permissions/service.py); else Refused,
        with `refused`."""
        state = self.state(sid)
        with state.lock:
            row = self.store.session(sid)
            if (state.runner is not None or self.store.has_queued(sid)
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

    def compact(self, sid, instructions=None):
        """The user's compaction of a session's context: at once for one at
        rest, on a thread of its own held as its runner, so no turn starts
        meanwhile and the client needn't wait for the handoff; before the
        next step for one whose turn runs or waits for an answer.
        `instructions`: the user's words on what the handoff keeps, or None.
        Its record (or `compaction_failed`) reaches the watchers as any
        record does."""
        instructions = (instructions or "").strip() or None
        if self.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        state = self.state(sid)
        with state.lock:
            if not self.accepting:
                raise Refused("the server is shutting down")
            row = self.store.session(sid)
            busy = (state.runner is not None or self.store.has_queued(sid)
                    or row["state"] in ("running", "waiting"))
            if busy:
                session = state.harness
                if session is None:
                    raise Refused("its turn waits for an answer; a compaction "
                                  "can be asked for once it runs again")
                session.want_compaction("user", instructions)
                return {"compacting": "before_next_step"}
            if not any(r["type"] == "user" for r in self.log(sid).branch()):
                raise Refused("the session has no conversation yet")
            thread = threading.Thread(target=self._compact,
                                      args=(sid, state, instructions),
                                      name=f"compact-{sid}", daemon=True)
            self.hold_runner(state, thread)
        thread.start()
        return {"compacting": "now"}

    def _compact(self, sid, state, instructions):
        lock = self.session_lock(sid)
        try:
            lock.acquire()
            try:
                self.harness(sid, self.log(sid, lock)).compact(
                    "user", instructions=instructions)
            finally:
                lock.release()
        except Exception:
            traceback.print_exc()
        finally:
            with state.lock:
                # Its last word is still to say, as a turn runner's is
                self.let_go(state, leaving=True)
        try:
            settled = self.settled(sid)
            if settled is not None:
                self.publish(sid, {"settled": settled})
            self.dispatch(sid)
        finally:
            self.said_last_word(state)

    def busy_with(self):
        """What keeps the server up besides its clients: turns running, resuming
        or saying their last word, messages that can start one, running jobs. A
        parked ask waits on disk and keeps nothing up."""
        states = self.all_states()
        turns = sorted(sid for sid, s in states if s.runner is not None or s.leaving)
        blocked = {sid for sid, s in states if s.blocked}
        queued = [s for s in self.store.waiting_sessions() if s not in blocked]
        reasons = {"turns": turns, "queued": sorted(set(queued) - set(turns))}
        for service in self.services.values():
            reasons.update(service.busy_with())
        return {name: value for name, value in reasons.items() if value}

    def queued_unblocked(self):
        """Sessions with a message that can start a turn, less those left to
        the next start's repair."""
        return set(self.store.waiting_sessions()) - self.blocked

    def wait(self, timeout=None):
        """True once no turn is running or about to start and nothing is queued
        (or stopping)."""
        deadline = None if timeout is None else time.monotonic() + timeout

        def left():
            return None if deadline is None else max(0.0, deadline - time.monotonic())

        while True:
            with self.activity:
                if not self.activity.wait_for(
                        lambda: not (self.runner_count or self.leaving_count),
                        left()):
                    return False
            if not self.accepting or not self.queued_unblocked():
                return True
            # A message no runner has taken yet: one is on its way, or none
            # comes before the deadline
            with self.activity:
                pause = WAIT_LOOK_SECONDS if deadline is None else min(
                    WAIT_LOOK_SECONDS, left())
                if not pause:
                    return False
                self.activity.wait(pause)

    def close_if_idle(self):
        """Take no new messages, but only if nothing is busy: True if the
        server may go. The inbox and the services are looked at outside the
        door (design rule 12), between two looks at what runs: what a
        service has at work is started by a turn or a message, which the
        second look sees by its counts, running or come and gone. The inbox
        is read once more under the door, at rest, for a notice a service
        committed between the two."""
        with self.door:
            if self.busy_now():
                return False
            held, admitted = self.runners_held, self.admissions
        if self.queued_unblocked():
            return False
        if any(v for service in self.services.values()
               for v in service.busy_with().values()):
            return False
        blocked = self.blocked
        with self.door:
            if (self.busy_now() or self.runners_held != held
                    or self.admissions != admitted):
                return False
            if set(self.store.waiting_sessions()) - blocked:
                return False
            self.accepting = False
            return True

    def stop(self, timeout=10):
        """Take no new messages; give running turns up to `timeout` to end.

        True if they did. A turn still running resumes at the next start,
        without using a try: this is a clean stop, not a crash.
        """
        with self.door:
            self.accepting = False
        with self.activity:
            drained = self.activity.wait_for(lambda: not self.runner_count, timeout)
        for _, state in self.all_states():
            running = state.running
            if running is not None:
                self.store.mark_clean_stop(running[1]["id"])
        # Then the services: each job's loss is written before it is killed
        for service in self.services.values():
            service.shutdown()
        return drained
