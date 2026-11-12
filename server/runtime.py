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
"""

import json
import threading
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
from server.services import Refused
from server.session_actions import (
    SessionActions, check_thinking_effort, new_id, title,
)
from server.store import Store, UnknownSession

FAILED = ("crashed", "cut_off", "failed")
MAX_RESUMES = 3
WHEN_BUSY = ("steer", "queue")
# What a turn begun by `continue` opens with: the user's wish, not their words
CONTINUE = "[The user asked you to go on with the task.]"
# Events a watch may have waiting; a watcher further behind is let go with
# a `bye` "behind", and follows again from the log, which holds them all
WATCH_QUEUE_MAX = 2000
# Why a watch the runtime lets go ends (protocol/messages.py)
BEHIND = "behind"


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
        # Harness sessions in memory, while the server runs: id → session
        self.live = {}
        # One runner thread per session with work: id → thread
        self.runners = {}
        # How many runners were ever held (hold_runner)
        self.runners_held = 0
        # Sessions whose runner has left `runners` and still has its last word
        # to say (settled, then dispatch), as id → how many: that dispatch may
        # start a turn on a message admitted meanwhile, so nothing is at rest
        # until it has
        self.leaving = {}
        self.running = {}
        self.blocked = set()
        # Watching connections: session id → {token: Watcher}
        self.watchers = {}
        # The reply streaming in, per session: its latest draft event
        self.drafts = {}
        # Each session's context meter, as its watchers last got it, and the
        # compact_at_tokens it was measured under
        self.meters = {}
        self.meter_lines = {}
        self.watch_lock = threading.Lock()
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
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
        for service in services:
            service.attach(self)
            self.services[service.name] = service
            self.messages.update(service.messages())
            self.looking_messages += tuple(service.looking_messages)

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
        self.drafts.pop(sid, None)
        self.publish(sid, {"record": record})
        self.meter(sid, record)

    def meter(self, sid, record):
        """The context's meter after a record, sent to the watchers when it
        changes: from the whole log the first time this server sees the
        session or when the line's setting has changed since, then record by
        record (core/history.py)."""
        compact_at = self.setting("compact_at_tokens")
        before = self.meters.get(sid)
        if before is None or self.meter_lines.get(sid) != compact_at:
            now = None
            for each in self.log(sid).branch():
                now = history.metered(now, each, compact_at)
        else:
            now = history.metered(before, record, compact_at)
        self.meters[sid] = now
        self.meter_lines[sid] = compact_at
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
        self.drafts[sid] = event
        self.publish(sid, event)

    def current_draft(self, sid):
        """The draft event of the reply streaming in now, or None."""
        return self.drafts.get(sid)

    def session_lock(self, sid):
        return SessionLock(self.paths.locks / f"{sid}.lock")

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
            sid = new_id()
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
        with self.lock:
            handled_as = "started" if new else self.handled_as(sid, message)
            if handled_as == "queued":
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
        return {"admitted": admitted, "session": sid, "handled_as": handled_as}

    def handled_as(self, sid, message):
        """What becomes of a message to this session, decided under the lock:
        `reply` to its open ask, `steered` or `queued` (a follow-up) while a
        turn runs or waits to, `started` otherwise."""
        info = self.store.session(sid)
        if info is None:
            raise Refused(f"no session {sid!r}")
        if info["state"] == "waiting" and self.store.open_ask(sid) is not None:
            return "reply"
        busy = sid in self.runners or sid in self.store.waiting_sessions()
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
            if sid in self.runners or sid in self.store.waiting_sessions():
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
        """Finish what a crash or a stop left, then run what is queued."""
        # The services first: jobs a crash cut off are told, so their notices are
        # repaired and run like any queued message
        for service in self.services.values():
            service.begin()
        sessions = dict.fromkeys(row["session"] for row in self.store.unfinished_rows())
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
            writer = self.live.get(row["session"])
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

    def dispatch(self):
        """Start a runner for each session with queued messages and none running."""
        with self.lock:
            if not self.accepting:
                return
            for sid in self.store.waiting_sessions():
                if sid not in self.runners and sid not in self.blocked:
                    thread = threading.Thread(
                        target=self.runner, args=(sid,), name=f"session-{sid}"
                    )
                    # A stop doesn't wait on a thread past the drain
                    thread.daemon = True
                    self.hold_runner(sid, thread)
                    thread.start()

    def hold_runner(self, sid, thread):
        """`self.lock` held: `thread` is the session's runner, so no other
        turn of it starts; counted, so a look at what is busy made in two
        steps sees one that came between (close_if_idle)."""
        self.runners[sid] = thread
        self.runners_held += 1

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
                # Past its last turn, the runner still has its last word to
                # say: `settled`, then a dispatch that starts a turn on a
                # message admitted meanwhile
                self.begin_leaving(sid)
                if not drained:
                    # Left to the next start's repair: its queued message no
                    # longer keeps this server up
                    self.blocked.add(sid)
                self.changed.notify_all()
        try:
            if drained:
                settled = self.settled(sid)
                if settled is not None:
                    self.publish(sid, {"settled": settled})
                self.dispatch()
        finally:
            with self.changed:
                self.end_leaving(sid)
                self.changed.notify_all()

    def begin_leaving(self, sid):
        """`self.changed` held: a thread of the session's has left `runners`
        with its last word still to say (`settled`, then a dispatch that may
        start a turn on a message admitted meanwhile)."""
        self.leaving[sid] = self.leaving.get(sid, 0) + 1

    def end_leaving(self, sid):
        """`self.changed` held: that thread has said it."""
        self.leaving[sid] -= 1
        if not self.leaving[sid]:
            del self.leaving[sid]

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
            delivered=lambda steer: self.store.mark_steer_done(steer["inbox"]),
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
                rounds=bool(payload.get("rounds")),
                started_from_seq=payload.get("started_from_seq"),
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
        state = "failed" if outcome in FAILED else "idle"
        with self.changed:
            self.store.finish(row["arrival"], sid, state)
            for stop in self.store.stops(sid, row):
                self.store.mark_steer_done(stop["id"])
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
        with self.changed:
            # A stop now belongs to the parked turn, not to the reply
            self.running[sid] = (control, turn_row)
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
        upgrade.upgrade_file(log, session.session_record())
        session.restore(log)
        self.session_built(sid, session)
        self.live[sid] = session

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
        self.live.pop(sid, None)
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
        if sid not in self.live:
            self.live[sid] = self.make_session(
                sid, info or self.store.session(sid), log=log or self.log(sid),
                ask=PARK,
            )
            self.session_built(sid, self.live[sid])
        elif log is not None:
            session = self.live[sid]
            session.log.lock = log.lock
            session.log.last_seq = None
            if session.log.branch():
                session.restore(session.log)
        return self.live[sid]

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
        with self.lock:
            return sid in self.runners or sid in self.store.waiting_sessions()

    def stop_turn(self, sid, msg_id, source):
        """Commit the stop before setting its flag or sending the ack."""
        with self.changed:
            if not self.accepting:
                raise Refused("the server is shutting down")
            seen = self.store.query("SELECT * FROM inbox WHERE id = ?", msg_id)
            if seen:
                if seen[0]["kind"] != "stop" or seen[0]["session"] != sid:
                    raise Refused("message id already used")
                return {"stopped": True, "withdrawn": []}
            running = self.running.get(sid)
            if running is None or not history.unfinished_turn(self.log(sid).branch()):
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
        what is left: the open ask's question, if any, and what the services
        leave (running jobs) (Pi's `agent_settled`)."""
        with self.lock:
            if sid in self.runners or sid in self.store.waiting_sessions():
                return None
        ask = self.store.open_ask(sid)
        left = {
            "ask": None if ask is None else {
                "id": ask["id"], "tool": ask["tool"], "command": ask["command"],
                "why": ask["why"],
            },
        }
        for service in self.services.values():
            left.update(service.settled(sid))
        return left

    def append_between_turns(self, sid, refused, type, **fields):
        """Append a record to a session's log while no turn runs or waits for
        an answer (a grant taken back, permissions/service.py); else Refused,
        with `refused`."""
        with self.lock:
            row = self.store.session(sid)
            if (sid in self.runners or sid in self.store.waiting_sessions()
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

    def compact(self, sid):
        """The user's compaction of a session's context: at once for one at
        rest, on a thread of its own held as its runner, so no turn starts
        meanwhile and the client needn't wait for the handoff; before the
        next step for one whose turn runs or waits for an answer. Its record
        (or `compaction_failed`) reaches the watchers as any record does."""
        with self.lock:
            if not self.accepting:
                raise Refused("the server is shutting down")
            row = self.store.session(sid)
            if row is None:
                raise Refused(f"no session {sid!r}")
            busy = (sid in self.runners or sid in self.store.waiting_sessions()
                    or row["state"] in ("running", "waiting"))
            if busy:
                session = self.live.get(sid)
                if session is None:
                    raise Refused("its turn waits for an answer; a compaction "
                                  "can be asked for once it runs again")
                session.compact_asked = "user"
                return {"compacting": "before_next_step"}
            if not any(r["type"] == "user" for r in self.log(sid).branch()):
                raise Refused("the session has no conversation yet")
            thread = threading.Thread(target=self._compact, args=(sid,),
                                      name=f"compact-{sid}", daemon=True)
            self.hold_runner(sid, thread)
        thread.start()
        return {"compacting": "now"}

    def _compact(self, sid):
        lock = self.session_lock(sid)
        try:
            lock.acquire()
            try:
                self.harness(sid, self.log(sid, lock)).compact("user")
            finally:
                lock.release()
        except Exception:
            traceback.print_exc()
        finally:
            with self.changed:
                del self.runners[sid]
                # Its last word is still to say, as a turn runner's is
                self.begin_leaving(sid)
                self.changed.notify_all()
        try:
            settled = self.settled(sid)
            if settled is not None:
                self.publish(sid, {"settled": settled})
            self.dispatch()
        finally:
            with self.changed:
                self.end_leaving(sid)
                self.changed.notify_all()

    def busy_with(self):
        """What keeps the server up besides its clients: turns running, resuming
        or saying their last word, messages that can start one, running jobs. A
        parked ask waits on disk and keeps nothing up."""
        with self.lock:
            turns = sorted(set(self.runners) | set(self.leaving))
            queued = [s for s in self.store.waiting_sessions() if s not in self.blocked]
        reasons = {"turns": turns, "queued": sorted(set(queued) - set(turns))}
        for service in self.services.values():
            reasons.update(service.busy_with())
        return {name: value for name, value in reasons.items() if value}

    def wait(self, timeout=None):
        """True once no turn is running or about to start and nothing is queued
        (or stopping)."""

        def idle():
            waiting = set(self.store.waiting_sessions()) - self.blocked
            return (not self.runners and not self.leaving
                    and (not self.accepting or not waiting))

        with self.changed:
            return self.changed.wait_for(idle, timeout)

    def close_if_idle(self):
        """Take no new messages, but only if nothing is busy: True if the
        server may go. The services are asked outside the lock that starting
        a turn takes (design rule 12), between two looks under it: what a
        service has at work is started by a turn, which the second look
        sees, running or come and gone."""
        with self.lock:
            if self.turns_busy():
                return False
            held = self.runners_held
        if any(v for service in self.services.values()
               for v in service.busy_with().values()):
            return False
        with self.lock:
            if self.turns_busy() or self.runners_held != held:
                return False
            self.accepting = False
            return True

    def turns_busy(self):
        """`self.lock` held: a turn runs, says its last word or can start."""
        queued = [s for s in self.store.waiting_sessions() if s not in self.blocked]
        return bool(self.runners or self.leaving or queued)

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
        # Then the services: each job's loss is written before it is killed
        for service in self.services.values():
            service.shutdown()
        return drained
