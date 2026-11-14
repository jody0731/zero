"""The server's server: one process for every session, a thin shell around the
runtime.

It holds server.lock for its whole life, so whoever holds it owns the socket: it
removes any socket left behind and listens on a Unix socket only the user can
open (0600 in a 0700 folder): no port, no password. A client sends one JSON
object on one line and gets one line back: the messages and their replies
are protocol/messages.py's. Each request goes through the same steps, the app
layer: who sends it (the socket's peer, else the app token: server/peers.py),
the protocol it is written in (its reply walked back to it:
protocol/older.py), its fields the message lacks (reported), its id run once
if it changes something (server/idempotency.py), then the kernel or a
service, its reply built from the store in one place (server/replies.py).

ZERO's own commands (a child of the server, or in its unit: server/peers.py) get
the looking messages only, never a send, a stop or a change: who may answer an ask
is the user's own client. A workflow, a command that calls ZERO, may also send a
service's workflow messages for its own session (subagent/send: a subagent of
that session, whose asks go where its other subagents' go).

The server leaves after `server_idle_minutes` with no turn, no message that could start
one, no job and no client connected (a watch sent with keeps_server_up false
doesn't count: a web page left open; it ends with bye "idle"); a parked ask
waits on disk. A shutdown or
SIGTERM drains: no new messages, running turns get a few seconds, each job's
loss is written before it is killed, and what is cut off resumes at the next
start without using a try. Nothing starts it at login: `zero` starts it when
none answers (client/launcher.py).
"""

import fcntl
import json
import os
import select
import signal
import socket
import threading
import time
import traceback
from contextlib import contextmanager

import settings
from core import history
from server import peers, replies
from server.idempotency import MessageReplies
from server.services import Refused
from server.runtime import Message
from protocol import messages, older, version

# How long a stop waits for running turns
DRAIN_SECONDS = 10
# How long a new server waits for an old one to let go of server.lock
LOCK_WAIT_SECONDS = 10
MAX_REQUEST = 1 << 20
# How long the list's changes gather before the list is sent again
LIST_GATHER_SECONDS = 0.1
# Seconds between two drafts on a watch: ten a second at most
DRAFT_EVERY_SECONDS = 0.1
# How long a watch's client may take to read what is sent before the watch
# is let go: a stalled client frees its thread (design rule 12)
WATCH_SEND_SECONDS = 30
STOP_REASONS = ("stopped", "restart")
# The messages the kernel serves itself; a service adds its own (server/services.py)
SERVED = ("server/hello", "server/stop", "session/send", "session/continue",
          "session/stop", "session/list", "session/read", "session/watch",
          "session/mark_seen", "session/rename", "session/archive",
          "session/unarchive", "session/delete", "session/set", "session/compact",
          "session/user_messages", "session/rewind", "session/fork",
          "setting/list", "setting/set")
# Those of them that only look, which ZERO's own commands may send too
# (peers.py); a service names its own in `looking`
LOOKING_MESSAGES = ("server/hello", "session/list", "session/read", "session/watch",
                    "session/user_messages")
SESSION_EDITS = ("session/mark_seen", "session/rename", "session/archive",
                 "session/unarchive", "session/delete", "session/set")
# The messages that change something whose id the inbox keeps, committed
# with the change itself (server/runtime.py); every other one's is
# message_replies' (server/idempotency.py)
INBOX_KEPT = ("session/send", "session/continue", "session/stop")
# What every request carries besides its message's own fields
ENVELOPE = ("message", "protocol")


class AlreadyRunning(Exception):
    pass


class Server:
    def __init__(self, runtime, paths, server_idle_minutes=None,
                 lock_wait=LOCK_WAIT_SECONDS):
        """`server_idle_minutes`: None reads config.toml's at each look, so a change
        in the settings view counts without a restart."""
        self.runtime = runtime
        self.paths = paths
        self.server_idle_minutes = server_idle_minutes
        self.lock_wait = lock_wait
        self.build = version.build_hash()
        self.started = time.time()
        self.stopping = threading.Event()
        # The `bye` its watches get when it stops
        self.reason = "stopped"
        # Open connections of every kind: a client connected keeps it up,
        # but for the watches sent with keeps_server_up false, counted apart
        self.connections = 0
        self.light_watches = 0
        self.connection_lock = threading.Lock()
        # Tells the user's clients from ZERO's own commands
        self.peers = peers.Peers()
        # Proves the user where the socket can't
        self.app_token = peers.AppToken(paths.app_token)
        # Each message that changes something, run once by its id
        self.message_replies = MessageReplies(runtime.store)
        # The release it runs, for hello and a restart's check
        self.release = version.release()

    def serve(self, ready=None):
        """Run until stop(), SIGTERM or the idle exit. `ready` is set once the
        socket listens."""
        self.paths.make()
        lock = self.take_lock()
        try:
            listener = self.listen()
            try:
                self.runtime.start()
                self.app_token.make()
                self.message_replies.start()
                # Signals reach the main thread only; tests serve from another
                if threading.current_thread() is threading.main_thread():
                    for sig in (signal.SIGTERM, signal.SIGINT):
                        signal.signal(sig, lambda *_: self.stop())
                if ready:
                    ready.set()
                self.accept(listener)
            finally:
                # Stop listening before letting go of the lock: a client that
                # connects now is refused and starts the next server
                listener.close()
                self.paths.socket.unlink(missing_ok=True)
            if self.stopping.is_set():
                print(f"ZERO's server: stopping ({self.reason}).", flush=True)
            self.runtime.stop(DRAIN_SECONDS)
            self.runtime.close_watchers(self.reason)
            self.wait_for_connections(2)
        finally:
            # Closing the file lets go of the lock
            lock.close()

    def stop(self, reason="stopped"):
        self.reason = reason
        self.stopping.set()

    def take_lock(self):
        """server.lock, waiting a while for an old server that is still exiting."""
        lock = open(self.paths.lock, "a+")
        deadline = time.monotonic() + self.lock_wait
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    lock.seek(0)
                    pid = lock.read().strip() or "?"
                    lock.close()
                    raise AlreadyRunning(
                        f"ZERO's server is already running (pid {pid})"
                    ) from None
                time.sleep(0.05)
        self.wait_for_old_server(deadline)
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        return lock

    def wait_for_old_server(self, deadline):
        """A server from before 0.6.2.8 holds host.lock while it runs: wait
        for it to exit as for server.lock, so two never share the socket."""
        path = self.paths.old_server_lock
        if not path.exists():
            return
        with open(path) as old:
            while True:
                try:
                    fcntl.flock(old, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(old, fcntl.LOCK_UN)
                    return
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        pid = old.read().strip() or "?"
                        raise AlreadyRunning(
                            f"ZERO's server from before 0.6.2.8 is still "
                            f"running (pid {pid})"
                        ) from None
                    time.sleep(0.05)

    def listen(self):
        path = self.paths.socket
        # One left by a server that died: the lock says nobody else uses it
        path.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # Born 0600, so there is no moment when others could connect
        umask = os.umask(0o177)
        try:
            listener.bind(str(path))
        finally:
            os.umask(umask)
        listener.listen()
        # Wake up now and then to see whether it's time to stop
        listener.settimeout(0.2)
        return listener

    def accept(self, listener):
        """Take connections until stopped or idle for long enough."""
        idle_since = time.monotonic()
        next_look = 0.0
        while not self.stopping.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                conn = None
            if conn is not None:
                # Counted here, in the one thread that accepts, so the idle
                # check below can't miss a connection being handed over
                with self.connection_lock:
                    self.connections += 1
                threading.Thread(
                    target=self.handle, args=(conn,), daemon=True
                ).start()
            now = time.monotonic()
            if now < next_look:
                continue
            idle_seconds = self.idle_seconds()
            # Look often enough to leave close to the idle time, at most each
            # second
            next_look = now + min(1.0, idle_seconds / 4)
            if self.holding() or self.runtime.busy_with():
                idle_since = None
            elif idle_since is None:
                idle_since = now
            elif now - idle_since >= idle_seconds and self.leave_idle():
                minutes = idle_seconds / 60
                print(f"ZERO's server: idle for {minutes:g} min, exiting.", flush=True)
                # The watches that didn't hold it end with this reason
                self.stop("idle")
                return

    def idle_seconds(self):
        minutes = (self.server_idle_minutes
                   or self.runtime.config()["server_idle_minutes"])
        return minutes * 60

    def holding(self):
        """The open connections that keep the server up."""
        return self.connections - self.light_watches

    def leave_idle(self):
        """Whether the server may go: checked once more, under the lock a new
        turn takes, so nothing starts in the moment it decides."""
        with self.connection_lock:
            if self.holding():
                return False
            return self.runtime.close_if_idle()

    @contextmanager
    def light(self, request):
        """A watch sent with keeps_server_up false, counted apart while it
        lasts, so it leaves the server free to go when idle."""
        keeps = request.get("keeps_server_up", True)
        if not isinstance(keeps, bool):
            raise Refused("keeps_server_up is true or false")
        if keeps:
            yield
            return
        with self.connection_lock:
            self.light_watches += 1
        try:
            yield
        finally:
            with self.connection_lock:
                self.light_watches -= 1

    def wait_for_connections(self, seconds):
        deadline = time.monotonic() + seconds
        while self.connections and time.monotonic() < deadline:
            time.sleep(0.02)

    def handle(self, conn):
        """One request, one reply, then the connection closes; or a watch."""
        try:
            with conn:
                self.serve_connection(conn)
        finally:
            with self.connection_lock:
                self.connections -= 1

    # A check every reply and event passes before it goes out, with its
    # message's name and kind ("reply", "event"): the tests hold each to
    # the schema (None: no check)
    reply_check = None

    def serve_connection(self, conn):
        conn.settimeout(10)
        name, protocol = None, version.PROTOCOL
        try:
            request = json.loads(read_line(conn))
            if not isinstance(request, dict):
                raise ValueError("a request is a JSON object")
            name = request.get("message")
            protocol = self.protocol_of(request)
            # The token proves, and goes no further
            token = request.pop("token", None)
            relayed_for = request.pop("relayed_for_pid", None)
            try:
                pid = peers.proven(conn, token, self.app_token)
                # A relay's client, checked as a socket's own peer is
                pid = peers.relayed(pid, relayed_for, self.peers)
            except PermissionError as why:
                raise Refused(str(why)) from None
            # ZERO's own commands may look, never act; a workflow sends for
            # its own session alone
            looking = (*LOOKING_MESSAGES, *self.runtime.looking_messages)
            if name not in looking and pid is not None:
                if self.peers.is_zero_command(pid):
                    self.check_workflow(name, request, pid, looking)
            if name == "session/watch":
                with self.light(request):
                    self.watch(conn, request, protocol)
                return
            if name == "session/list" and request.get("watch"):
                with self.light(request):
                    self.watch_list(conn, request, protocol)
                return
            reply = self.reply_to(name, request, protocol)
        except Refused as e:
            reply = error(str(e))
        except (OSError, ValueError) as e:
            reply = {"ok": False, "error": f"bad request: {e}"}
        except Exception as e:
            traceback.print_exc()
            reply = {"ok": False, "error": f"internal error: {type(e).__name__}"}
        try:
            self.send_out(conn, name, reply, protocol)
        except OSError:
            pass

    @staticmethod
    def protocol_of(request):
        """The protocol a request is written in (none named: 4), as its
        reply goes out in it; Refused for one older than the oldest."""
        asked = request.get("protocol", version.OLDEST)
        if type(asked) is not int:
            raise Refused("protocol is a whole number")
        protocol = older.answered_in(asked)
        if protocol is None:
            raise Refused(f"this server reads protocols {version.OLDEST} to "
                          f"{version.PROTOCOL}, not {asked}")
        return protocol

    def send_out(self, conn, name, reply, protocol, kind="reply"):
        """A reply or event as this protocol writes it, walked back to the
        sender's."""
        if self.reply_check is not None:
            self.reply_check(name, reply, kind)
        reply = older.reply_in(protocol, name, reply)
        conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode())

    def send_event(self, conn, name, event, protocol):
        """One event of a watch (`name`'s), as apps read it: a record's
        parts an app shows added (replies.event)."""
        if name == "session/watch":
            event = replies.event(event)
        self.send_out(conn, name, event, protocol, "event")

    def reply_to(self, name, request, protocol):
        """The reply to one request: its id run once when it changes
        something, its fields the message lacks reported."""
        spec = messages.MESSAGES.get(name)
        unknown = []
        if spec is not None:
            known = {*ENVELOPE, *spec["fields"]}
            unknown = sorted(field for field in request if field not in known)
        if spec is not None and spec["changes"] and name not in INBOX_KEPT:
            msg_id = request.get("id")
            if msg_id is None and protocol >= 5:
                reply = error(f"{name} changes something, so it carries an "
                              "id: a string the sender picks, new for each "
                              "message")
            elif msg_id is None:
                reply = self.answered(request, protocol)
            elif not isinstance(msg_id, str) or not 0 < len(msg_id) <= 200:
                reply = error("a message's id is a string of 1 to 200 "
                              "characters")
            else:
                reply = self.message_replies.run(
                    name, request, lambda r: self.answered(r, protocol))
        else:
            reply = self.answered(request, protocol)
        if unknown:
            reply = {**reply, "unknown_fields": unknown}
        return reply

    def answered(self, request, protocol):
        """answer()'s reply, every failure a reply too."""
        try:
            return self.answer(request, protocol)
        except Refused as e:
            return error(str(e))
        except (OSError, ValueError) as e:
            return {"ok": False, "error": f"bad request: {e}"}
        except Exception as e:
            traceback.print_exc()
            return {"ok": False, "error": f"internal error: {type(e).__name__}"}

    def check_workflow(self, name, request, pid, looking):
        """Refused unless one of ZERO's own commands sends a workflow's
        message naming, in `for_session`, the session whose command it is."""
        workflow = self.runtime.workflow_messages
        if name not in workflow:
            raise Refused(peers.refused(looking, workflow))
        named = request.get("for_session")
        owner = self.runtime.command_session(peers.unix_session(pid))
        if owner is None or named != owner:
            raise Refused(peers.not_its_session(named, owner))

    def tree_message(self, name, request):
        """The conversation's tree: the user's messages to begin from, a
        rewind to before one, a fork into a new session."""
        sid, seq = request.get("session"), request.get("seq")
        if not isinstance(sid, str):
            return error(f"{name} needs a session string")
        if seq is not None and (not isinstance(seq, int) or isinstance(seq, bool)):
            return error("seq is a record's number")
        try:
            if name == "session/user_messages":
                return {"ok": True, "messages": self.runtime.rewind_points(sid)}
            if name == "session/rewind":
                if seq is None:
                    return error("session/rewind needs the seq of one of the "
                                 "user's messages")
                done = self.runtime.rewind(sid, seq)
                return {"ok": True, **done, "rewind": replies.record(done["rewind"])}
            source = request.get("source")
            return {"ok": True, **self.runtime.fork(
                sid, seq, source if isinstance(source, str) else "user"
            )}
        except Refused as exc:
            return error(str(exc))

    def answer(self, request, protocol=version.PROTOCOL):
        if not isinstance(request, dict):
            return error("a request is a JSON object")
        name = request.get("message")
        if name == "server/hello":
            return self.hello(protocol)
        if name == "session/send":
            return self.send(request)
        if name == "session/stop":
            sid = request.get("session")
            source = request.get("source")
            if not isinstance(sid, str) or not isinstance(source, str):
                return error("session and source must be strings")
            msg_id = request.get("id")
            if not isinstance(msg_id, str) or not 0 < len(msg_id) <= 200:
                return error("a stop needs an id: a string the sender picks")
            try:
                return {"ok": True, **self.runtime.stop_turn(sid, msg_id, source)}
            except Refused as exc:
                return error(str(exc))
        if name == "session/compact":
            sid = request.get("session")
            if not isinstance(sid, str):
                return error("session/compact needs a session string")
            words = request.get("instructions")
            if words is not None and not isinstance(words, str):
                return error("session/compact's instructions must be a string")
            try:
                return {"ok": True, **self.runtime.compact(sid, words)}
            except Refused as exc:
                return error(str(exc))
        if name in ("session/user_messages", "session/rewind", "session/fork"):
            return self.tree_message(name, request)
        if name == "session/continue":
            sid, msg_id = request.get("session"), request.get("id")
            source = request.get("source")
            if not isinstance(sid, str) or not isinstance(source, str):
                return error("session and source must be strings")
            if not isinstance(msg_id, str) or not 0 < len(msg_id) <= 200:
                return error("a continue needs an id: a string the sender picks")
            return {"ok": True, **self.runtime.continue_turn(sid, msg_id, source)}
        if name == "session/list":
            archived, workspace = list_filters(request)
            return {"ok": True, "sessions": self.runtime.sessions(archived, workspace)}
        if name == "session/read":
            before = request.get("before")
            if before is not None and type(before) is not int:
                return error("before must be an integer")
            sid, after, limit = read_range(request)
            records, earlier_count = self.runtime.history(sid, after, limit, before)
            return {"ok": True, "records": replies.records(records),
                    "earlier_count": earlier_count,
                    "runs_with": self.runtime.runs_with(sid),
                    "context": self.runtime.context_meter(sid)}
        if name in SESSION_EDITS:
            return self.edit_session(name, request)
        if name in self.runtime.messages:
            # A service's message
            try:
                return self.runtime.messages[name](request)
            except Refused as exc:
                return error(str(exc))
        if name == "setting/list":
            return self.settings()
        if name == "setting/set":
            setting = request.get("name")
            if not isinstance(setting, str):
                return error("setting/set needs a setting's name, a string")
            config = self.config()
            problem = config.set(setting, request.get("value"))
            if problem:
                return error(problem)
            return {"ok": True, "set": {setting: config.values[setting]}}
        if name == "server/stop":
            return self.shutdown(request, protocol)
        served = {*SERVED, *self.runtime.messages}
        known = ", ".join(m for m in messages.MESSAGES if m in served)
        return error(f"unknown message {name!r}; messages: {known}")

    def hello(self, protocol):
        """The server's versions and what keeps it busy, in the client's
        protocol (older.py walks it back)."""
        return {
            "ok": True, "protocol": protocol,
            "server_protocol": version.PROTOCOL, "version": self.release,
            "build": self.build,
            "pid": os.getpid(), "started_at": self.started,
            "busy_with": self.runtime.busy_with(),
            "last_session": self.runtime.last(),
            "mode": self.runtime.mode, "model": self.runtime.model,
            # What a client offers and shows without loading the harness
            "modes": [dict(row) for row in self.runtime.mode_rows],
            "default_model": self.runtime.default_model,
        }

    def config(self):
        """The server's config.toml (settings.Config); Refused for a server that
        reads none (the evals')."""
        config = self.runtime.config
        if not isinstance(config, settings.Config):
            raise Refused("this server reads no config.toml")
        return config

    def settings(self):
        """Every setting of ZERO's with its value, for the settings view."""
        config = self.config()
        return {"ok": True, "settings": config.listing(), "error": config.error}

    def edit_session(self, name, request):
        """A session's seen, rename, archive, unarchive, delete and set."""
        sid = request.get("session")
        if not isinstance(sid, str):
            return error(f"{name} needs a session string")
        if name == "session/mark_seen":
            seq = request.get("seq")
            if type(seq) is not int or seq < 0:
                return error("session/mark_seen needs a seq, a nonnegative integer")
            self.runtime.mark_seen(sid, seq)
        elif name == "session/rename":
            text = request.get("title")
            if not isinstance(text, str):
                return error("session/rename needs a title string")
            self.runtime.rename(sid, text)
        elif name in ("session/archive", "session/unarchive"):
            self.runtime.archive(sid, name == "session/archive")
        elif name == "session/set":
            values = {name: request.get(name) for name in history.SWITCHABLE}
            if any(v is not None and not isinstance(v, str) for v in values.values()):
                return error(f"session/set takes "
                             f"{', '.join(history.SWITCHABLE)}, each a string")
            return {"ok": True, **self.runtime.set_session(sid, **values)}
        else:
            return {"ok": True, "trash": self.runtime.delete(sid)}
        return {"ok": True}

    def shutdown(self, request, protocol=version.PROTOCOL):
        """Stop, or say what runs: `force` stops anyway. A restart for a newer
        build is never forced, and never done for a client of an older
        protocol or release: it would put older code in this one's place
        (an older client then goes on with this server)."""
        force = request.get("force", False)
        reason = request.get("reason", "stopped")
        if not isinstance(force, bool) or reason not in STOP_REASONS:
            return error(f"server/stop takes force (boolean) and reason {STOP_REASONS}")
        busy = self.runtime.busy_with()
        if reason == "restart" and self.newer_than(request, protocol):
            return {"ok": True, "stopping": False, "busy_with": busy}
        if busy and (not force or reason == "restart"):
            return {"ok": True, "stopping": False, "busy_with": busy}
        self.stop(reason)
        return {"ok": True, "stopping": True}

    def newer_than(self, request, protocol):
        """Whether this server is newer than the client asking it to stop:
        an older protocol, or an older release named in `version`."""
        if protocol < version.PROTOCOL:
            return True
        theirs = request.get("version")
        if not isinstance(theirs, str):
            return False
        return version.release_order(theirs) < version.release_order(self.release)

    def send(self, request):
        msg_id, text = request.get("id"), request.get("text")
        if not isinstance(msg_id, str) or not 0 < len(msg_id) <= 200:
            return error("a message needs an id: a string the sender picks")
        if not isinstance(text, str):
            return error("a message needs its text, a string")
        names = ("session", "mode", "model", "thinking_effort", "reply_to", "workspace",
                 "choice")
        fields = {name: request.get(name) for name in names}
        fields["when_busy"] = request.get("when_busy") or "steer"
        fields["source"] = request.get("source")
        if not isinstance(fields["source"], str):
            return error("source must be a string")
        for name, value in fields.items():
            if value is not None and not isinstance(value, str):
                return error(f"{name} must be a string")
        # The fields the services read (rounds/'s `rounds`), each of the
        # kind the protocol declares for it; a false one is left out
        declared = messages.MESSAGES["session/send"]["fields"]
        extra = {}
        for name in self.runtime.message_fields():
            value = request.get(name)
            if name not in declared or value is None:
                continue
            kind = declared[name].get("type")
            if kind == "boolean" and not isinstance(value, bool):
                return error(f"{name} must be true or false")
            if value is not False:
                extra[name] = value
        try:
            ack = self.runtime.admit(Message(msg_id, text, **fields),
                                     extra or None)
        except Refused as e:
            return error(str(e))
        return {"ok": True, **ack}

    def watch(self, conn, request, protocol=version.PROTOCOL):
        """The records after a seq, then each new one as it is appended: an ask
        is its `ask` record. Ends with a `bye` when the server stops."""
        sid, after, limit = read_range(request, limit=None)

        def emit(event):
            self.send_event(conn, "session/watch", event, protocol)

        try:
            token, watcher = self.runtime.subscribe(sid)
        except Refused as exc:
            emit(error(str(exc)))
            return
        conn.settimeout(WATCH_SEND_SECONDS)
        try:
            # Subscribed first, so a record appended meanwhile is in the queue
            # and the seq check drops the copy
            records, earlier_count = self.runtime.history(sid, after, limit)
            if earlier_count:
                emit({"earlier_count": earlier_count})
            # The meter as the whole log has it, for a watch that starts late
            meter = self.runtime.context_meter(sid)
            if meter is not None:
                emit({"context": meter})
            for record in records:
                emit({"record": record})
                after = record["seq"]
            settled = self.runtime.settled(sid)
            if settled is not None:
                emit({"settled": settled})
            # A watch that starts mid-reply gets the reply so far
            draft = self.runtime.current_draft(sid)
            if draft is not None:
                emit(draft)
            while True:
                drafted = False
                for event in watcher.take(0.2):
                    record = event.get("record")
                    if record is not None:
                        if record["seq"] <= after:
                            continue
                        after = record["seq"]
                    drafted = drafted or "draft" in event
                    emit(event)
                if drafted:
                    # At most DRAFT_EVERY_SECONDS a draft: the newest replaces what
                    # queues meanwhile (Watcher.put)
                    time.sleep(DRAFT_EVERY_SECONDS)
                if watcher.bye:
                    emit({"bye": watcher.bye})
                    return
                readable, _, _ = select.select([conn], [], [], 0)
                if readable:
                    # There is only one request on a watch connection; EOF leaves
                    conn.recv(1)
                    return
        except (OSError, ValueError, Refused):
            return
        finally:
            self.runtime.unsubscribe(sid, token)

    def watch_list(self, conn, request, protocol=version.PROTOCOL):
        """The list, then the whole list again whenever it changed, its changes
        gathered for a moment; it ends with a `bye` when the server stops."""

        def emit(event):
            self.send_event(conn, "session/list", event, protocol)

        try:
            archived, workspace = list_filters(request)
        except Refused as exc:
            emit(error(str(exc)))
            return
        conn.settimeout(WATCH_SEND_SECONDS)
        sent = None
        version = self.runtime.list_version
        try:
            while True:
                sessions = self.runtime.sessions(archived, workspace)
                if sessions != sent:
                    emit({"sessions": sessions})
                    sent = sessions
                while True:
                    if self.stopping.is_set():
                        emit({"bye": self.reason})
                        return
                    readable, _, _ = select.select([conn], [], [], 0)
                    if readable:
                        conn.recv(1)
                        return
                    now = self.runtime.wait_list(version, 0.2)
                    if now != version:
                        version = now
                        time.sleep(LIST_GATHER_SECONDS)
                        version = self.runtime.list_version
                        break
        except (OSError, ValueError):
            return

def list_filters(request):
    """session/list's `archived` (False when left out) and `workspace`."""
    archived = request.get("archived", False)
    workspace = request.get("workspace")
    if archived is not None and not isinstance(archived, bool):
        raise Refused("archived must be true, false or null")
    if workspace is not None and not isinstance(workspace, str):
        raise Refused("workspace must be a string")
    return archived, workspace


def read_range(request, limit=50):
    """A request's session, `after` (a seq) and `limit` (None: all; 0: none,
    for what a reply says beside the records)."""
    sid = request.get("session")
    after = request.get("after", 0)
    limit = request.get("limit", limit)
    if not isinstance(sid, str):
        raise Refused("this message needs a session string")
    if type(after) is not int or after < 0:
        raise Refused("after must be a nonnegative integer")
    if limit is not None and (type(limit) is not int or limit < 0):
        raise Refused("limit must be a nonnegative integer")
    return sid, after, limit


def error(text):
    return {"ok": False, "error": text}


def read_line(conn):
    data = b""
    while not data.endswith(b"\n"):
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_REQUEST:
            raise ValueError("request too long")
    return data.decode()
