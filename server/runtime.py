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
import threading
import time
import weakref
import traceback

import settings
from core import attribution, entries, timeline
from server import replies
from server.admission import (
    Admission, CONTINUE, Message, WHEN_BUSY, picked, sent_before,
)
from server.rebuild import FAILED
from server.services import Refused
from server.session_actions import SessionActions, title
from server.session_state import SessionState
from server.store import Store
from server.turn_runner import MAX_RESUMES, TurnRunner, switch_note
from server.watchers import BEHIND, DELETED, WATCH_QUEUE_MAX, Watcher, WatcherHub
from sessions import upgrade
from sessions.lock import Busy, SessionLock
from sessions.log import Log

WAIT_LOOK_SECONDS = 0.05


class Runtime(SessionActions):
    def __init__(self, paths, make_session, modes, services=(), model=None,
                 mode=None, config=None, default_model=None):
        """`make_session(sid, info, log=None, ask=PARK)` builds a harness. An
        upgrade supplies a fresh log to obtain the current header without writes.
        `modes`: every mode, least to most permissive, as its row
        (protocol/messages.py's row.mode) or its name; a session's asks
        go to the clients following it (they park). `services`: the components'
        (server/services.py), attached once the store is open.
        `config()`: config.toml's settings as they stand (settings.Config), read
        for each new session: its mode and model unless the message names them.
        `model` and `mode` win over the config's. `default_model`: the model
        where neither the message nor the config names one."""
        paths.make()
        self.paths = paths
        # Each mode as apps read it (name, about, in_cycle): a name alone is
        # a mode with nothing said of it, which a key steps to
        self.mode_rows = tuple(
            dict(m) if isinstance(m, dict)
            else {"name": m, "about": "", "in_cycle": True} for m in modes)
        self.modes = tuple(m["name"] for m in self.mode_rows)
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
        self.watcher_hub = WatcherHub(self)
        self.list_cache_version = None
        self.list_cache_rows = {}
        self.list_cache_lock = threading.Lock()
        self.list_build_lock = threading.Lock()
        self.admission = Admission(self)
        self.turn_runner = TurnRunner(self)
        self.store.on_change = self.notify_list_changed
        # The components' services by name, the messages they add, and those of
        # them that only look
        self.services = {}
        self.messages = {}
        self.streaming_messages = {}
        self.looking_messages = ()
        self.workflow_messages = ()
        self.principal_messages = ()
        self.features = ()
        for service in services:
            service.attach(self)
            self.services[service.name] = service
            self.messages.update(service.messages())
            self.streaming_messages.update(service.streaming_messages())
            self.looking_messages += tuple(service.looking_messages)
            self.workflow_messages += tuple(service.workflow_messages)
            self.principal_messages += tuple(service.principal_messages)
            self.features += tuple(
                f for f in service.features if f not in self.features
            )

    @property
    def model(self):
        """A new session's model, where its first message names none."""
        return self._model or self.config()["model"] or self.default_model

    @property
    def mode(self):
        return self._mode or self.config()["mode"]

    def unattended(self, sid):
        return any(service.unattended(sid)
                   for service in self.services.values())

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

    @property
    def list_version(self):
        return self.watcher_hub.list_version

    def notify_list_changed(self, changed_sids=()):
        return self.watcher_hub.notify_list_changed(changed_sids)

    def wait_list(self, version, timeout):
        return self.watcher_hub.wait_list(version, timeout)

    def wake(self, sid=None):
        """Dispatch an admitted notice to its session, or look for waiting ones."""
        if self.started:
            self.dispatch(sid)

    def log(self, sid, lock=None):
        """A session's log; what it appends reaches the session's watchers."""
        log = Log(self.paths.sessions / f"{sid}.jsonl", lock=lock,
                  transform=self.redact_entry)
        writer = weakref.ref(log)
        log.on_append = lambda record: self.appended(sid, record, writer())
        return log

    def read_log(self, sid):
        """Use the harness's log cache where this runtime already has one."""
        state = self.states.get(sid)
        harness = state.harness if state is not None else None
        return harness.log if harness is not None else self.log(sid)

    def read_records(self, sid):
        return upgrade.read_records(self.read_log(sid))

    def read_header(self, sid):
        try:
            return upgrade.read_header(self.read_log(sid))
        except timeline.NewerLog:
            return None

    def read_branch(self, sid, *, log=None):
        log = log or self.read_log(sid)
        try:
            records = log.branch()
            if not records or timeline.log_format(records) == timeline.LOG_FORMAT:
                return records
        except timeline.OldLog:
            pass
        return timeline.branch(upgrade.read_records(log))

    @staticmethod
    def read_log_record(line, *, seq=None):
        """Decode a log line for components that read from a byte offset."""
        raw = json.loads(line.decode("utf-8"))
        if seq == 1:
            timeline.marked_format(raw, first=True)
        return upgrade.read_record(raw, seq=seq)

    def redact(self, value):
        for service in self.services.values():
            value = service.redact(value)
        return value

    def redact_entry(self, entry):
        for service in self.services.values():
            entry = service.redact_entry(entry)
        return entry

    @property
    def private_ask_kinds(self):
        kinds = {}
        for service in self.services.values():
            kinds.update(service.private_ask_kinds)
        return kinds

    def stream_filters(self):
        """Fresh stream-filter callables provided by attached services."""
        found = []
        for service in self.services.values():
            factory = service.stream_filter()
            if factory is not None:
                found.append(factory())
        return found

    def appended(self, sid, record, log):
        if entries.has(record.entry, "turn_end"):
            self.store.mark_ended(sid, record.seq)
        # Any record ends the draft: the reply's own, a retry, a stop's note
        self.state(sid).draft = None
        self.publish(sid, {"record": record})
        self.meter(sid, log)

    def meter(self, sid, log=None):
        return self.watcher_hub.meter(sid, log)

    def draft(self, sid, step, draft):
        return self.watcher_hub.draft(sid, step, draft)

    def current_draft(self, sid):
        return self.watcher_hub.current_draft(sid)

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
        core's (attribution.MESSAGE_FIELDS) and those the services read."""
        names = list(attribution.MESSAGE_FIELDS)
        for service in self.services.values():
            names += [n for n in service.message_fields if n not in names]
        return names

    def in_workflow_scope(self, sid, scope):
        seen = set()
        while sid and sid not in seen:
            seen.add(sid)
            row = self.store.session(sid)
            if row is None or row.get("started_by") != "zero":
                return False
            sid = row.get("parent")
            if sid == scope:
                return True
        return False

    def prepare_message(self, message, extra, *, principal=None):
        prepared = []
        for service in self.services.values():
            item = service.prepare_message(message, extra, principal=principal)
            if item is not None:
                prepared.append(item)
        return prepared

    def message_label(self, entry):
        for service in self.services.values():
            label = service.message_label(entry)
            if label:
                return label
        return ""

    def message_restore(self, entry):
        restored = {}
        for service in self.services.values():
            item = service.message_restore(entry)
            if item:
                restored.update(item)
        return restored

    def admit(self, message, extra=None, *, legacy_send=False, principal=None):
        return self.admission.admit(
            message, extra, legacy_send=legacy_send, principal=principal,
        )

    def check_reply(self, sid, message, expected):
        return self.admission.check_reply(sid, message, expected)

    def admit_to_session(self, message, sid, new, payload, legacy_send,
                         claims=(), *, legacy_payload=None, principal=None):
        return self.admission.admit_to_session(
            message, sid, new, payload, legacy_send, claims=claims,
            legacy_payload=legacy_payload, principal=principal,
        )

    def handled_as(self, sid, state, message, legacy_send=False):
        return self.admission.handled_as(sid, state, message, legacy_send)

    def continue_turn(self, sid, msg_id, source):
        return self.admission.continue_turn(sid, msg_id, source)

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
        return self.turn_runner.adopt_logs()

    def rebuild_ahead(self):
        return self.turn_runner.rebuild_ahead()

    def repair(self, row, lock=None):
        return self.turn_runner.repair(row, lock)

    def _repair(self, row, lock):
        return self.turn_runner._repair(row, lock)

    def repair_end(self, log, pending, row, outcome, detail, stopped=False):
        return self.turn_runner.repair_end(log, pending, row, outcome, detail, stopped)

    def dispatch(self, sid=None):
        return self.turn_runner.dispatch(sid)

    def start_runner(self, sid):
        return self.turn_runner.start_runner(sid)

    def runner(self, sid, state):
        return self.turn_runner.runner(sid, state)

    def run_queued(self, sid, state, lock):
        return self.turn_runner.run_queued(sid, state, lock)

    def run_turn(self, sid, row, lock=None):
        return self.turn_runner.run_turn(sid, row, lock)

    def _run_turn(self, sid, row, lock, control):
        return self.turn_runner._run_turn(sid, row, lock, control)

    def end_for_missing_workspace(self, sid, row, log, records, begun, text, workspace,
                                  *, child_entries=()):
        return self.turn_runner.end_for_missing_workspace(
            sid, row, log, records, begun, text, workspace,
            child_entries=child_entries,
        )

    def finish_turn(self, sid, session, turn, row):
        return self.turn_runner.finish_turn(sid, session, turn, row)

    def take_reply(self, sid, row, ask, turn_id, log, control):
        return self.turn_runner.take_reply(sid, row, ask, turn_id, log, control)

    def reply_step(self, sid, ask, payload):
        return self.turn_runner.reply_step(sid, ask, payload)

    def upgrade_session(self, sid, log):
        return self.turn_runner.upgrade_session(sid, log)

    def harness_for_turn(self, sid, log, records, begun):
        return self.turn_runner.harness_for_turn(sid, log, records, begun)

    def harness(self, sid, log=None, info=None):
        return self.turn_runner.harness(sid, log, info)

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
        return self.admission.stop_turn(sid, msg_id, source)

    def subscribe(self, sid):
        return self.watcher_hub.subscribe(sid)

    def unsubscribe(self, sid, token):
        return self.watcher_hub.unsubscribe(sid, token)

    def publish(self, sid, event):
        return self.watcher_hub.publish(sid, event)

    def close_watchers(self, reason):
        return self.watcher_hub.close_watchers(reason)

    def forget_state(self, sid):
        return self.watcher_hub.forget(sid)

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
        left = {"ask": None if ask is None else replies.ask_row(ask)}
        for service in self.services.values():
            left.update(service.settled(sid))
        return left

    def append_between_turns(self, sid, refused, type, **fields):
        return self.turn_runner.append_between_turns(sid, refused, type, **fields)

    def append_side_record(self, sid, entry, *, timeout=10.0):
        return self.turn_runner.append_side_record(sid, entry, timeout=timeout)

    def run_on_session(self, sid, name, fn, *, if_busy=None, precheck=None):
        return self.turn_runner.run_on_session(
            sid, name, fn, if_busy=if_busy, precheck=precheck,
        )

    def _run_on_session(self, sid, state, fn):
        return self.turn_runner._run_on_session(sid, state, fn)

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
