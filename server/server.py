"""The server's server: one process for every session, a thin shell around the
runtime.

It holds server.lock for its whole life, so whoever holds it owns the socket: it
removes any socket left behind and listens on a Unix socket only the user can
open (0600 in a 0700 folder): no port, no password. A client sends one JSON
object on one line and gets one line back: the messages and their replies
are protocol/messages.py's.

ZERO's own commands (a child of the server, or in its unit: server/peers.py) get
the looking messages only, never a send, a stop or a change: who may answer an ask
is the user's own client.

The server leaves after `server_idle_minutes` with no turn, no message that could start
one, no job and no client connected; a parked ask waits on disk. A shutdown or
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

import settings
from core import history
from server import peers
from server.services import Refused
from server.runtime import Message
from protocol import messages, version

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
        # Open connections of every kind: a client connected keeps it up
        self.connections = 0
        self.connection_lock = threading.Lock()
        # Tells the user's clients from ZERO's own commands
        self.peers = peers.Peers()

    def serve(self, ready=None):
        """Run until stop(), SIGTERM or the idle exit. `ready` is set once the
        socket listens."""
        self.paths.make()
        lock = self.take_lock()
        try:
            listener = self.listen()
            try:
                self.runtime.start()
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
            if self.connections or self.runtime.busy_with():
                idle_since = None
            elif idle_since is None:
                idle_since = now
            elif now - idle_since >= idle_seconds and self.leave_idle():
                minutes = idle_seconds / 60
                print(f"ZERO's server: idle for {minutes:g} min, exiting.", flush=True)
                return

    def idle_seconds(self):
        minutes = (self.server_idle_minutes
                   or self.runtime.config()["server_idle_minutes"])
        return minutes * 60

    def leave_idle(self):
        """Whether the server may go: checked once more, under the lock a new
        turn takes, so nothing starts in the moment it decides."""
        with self.connection_lock:
            if self.connections:
                return False
            return self.runtime.close_if_idle()

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

    def serve_connection(self, conn):
        conn.settimeout(10)
        try:
            request = json.loads(read_line(conn))
            name = request.get("message") if isinstance(request, dict) else None
            # ZERO's own commands may look, never act
            looking = (*LOOKING_MESSAGES, *self.runtime.looking_messages)
            if (name not in looking
                    and self.peers.is_zero_command(peers.peer_pid(conn))):
                raise Refused(peers.refused(looking))
            if name == "session/watch":
                self.watch(conn, request)
                return
            if name == "session/list" and request.get("watch"):
                self.watch_list(conn, request)
                return
            reply = self.answer(request)
        except Refused as e:
            reply = error(str(e))
        except (OSError, ValueError) as e:
            reply = {"ok": False, "error": f"bad request: {e}"}
        except Exception as e:
            traceback.print_exc()
            reply = {"ok": False, "error": f"internal error: {type(e).__name__}"}
        try:
            conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode())
        except OSError:
            pass

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
                return {"ok": True, **self.runtime.rewind(sid, seq)}
            source = request.get("source")
            return {"ok": True, **self.runtime.fork(
                sid, seq, source if isinstance(source, str) else "user"
            )}
        except Refused as exc:
            return error(str(exc))

    def answer(self, request):
        if not isinstance(request, dict):
            return error("a request is a JSON object")
        name = request.get("message")
        if name == "server/hello":
            return self.hello(request)
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
            try:
                return {"ok": True, **self.runtime.compact(sid)}
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
            return {"ok": True, "records": records, "earlier_count": earlier_count,
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
            return self.shutdown(request)
        served = {*SERVED, *self.runtime.messages}
        known = ", ".join(m for m in messages.MESSAGES if m in served)
        return error(f"unknown message {name!r}; messages: {known}")

    def hello(self, request):
        theirs = request.get("protocol")
        if theirs is not None and theirs != version.PROTOCOL:
            return {
                **error(
                    f"this server speaks protocol {version.PROTOCOL}, the client "
                    f"{theirs}: restart the server with the client's code"
                ),
                "protocol": version.PROTOCOL,
            }
        return {
            "ok": True, "protocol": version.PROTOCOL, "build": self.build,
            "pid": os.getpid(), "started_at": self.started,
            "busy_with": self.runtime.busy_with(),
            "last_session": self.runtime.last(),
            "mode": self.runtime.mode, "model": self.runtime.model,
            # What a client offers and shows without loading the harness
            "modes": list(self.runtime.modes),
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
                return error("session/set takes mode, model, workspace, "
                             "thinking_effort, judge_model and "
                             "judge_thinking_effort, each a string")
            return {"ok": True, **self.runtime.set_session(sid, **values)}
        else:
            return {"ok": True, "trash": self.runtime.delete(sid)}
        return {"ok": True}

    def shutdown(self, request):
        """Stop, or say what runs: `force` stops anyway. A restart for a newer
        build is never forced."""
        force = request.get("force", False)
        reason = request.get("reason", "stopped")
        if not isinstance(force, bool) or reason not in STOP_REASONS:
            return error(f"server/stop takes force (boolean) and reason {STOP_REASONS}")
        busy = self.runtime.busy_with()
        if busy and (not force or reason == "restart"):
            return {"ok": True, "stopping": False, "busy_with": busy}
        self.stop(reason)
        return {"ok": True, "stopping": True}

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
        rounds = request.get("rounds")
        if rounds is not None and not isinstance(rounds, bool):
            return error("rounds must be true or false")
        extra = {"rounds": True} if rounds else None
        try:
            ack = self.runtime.admit(Message(msg_id, text, **fields), extra)
        except Refused as e:
            return error(str(e))
        return {"ok": True, **ack}

    def watch(self, conn, request):
        """The records after a seq, then each new one as it is appended: an ask
        is its `ask` record. Ends with a `bye` when the server stops."""
        sid, after, limit = read_range(request, limit=None)
        try:
            token, watcher = self.runtime.subscribe(sid)
        except Refused as exc:
            self.emit(conn, error(str(exc)))
            return
        conn.settimeout(WATCH_SEND_SECONDS)
        try:
            # Subscribed first, so a record appended meanwhile is in the queue
            # and the seq check drops the copy
            records, earlier_count = self.runtime.history(sid, after, limit)
            if earlier_count:
                self.emit(conn, {"earlier_count": earlier_count})
            # The meter as the whole log has it, for a watch that starts late
            meter = self.runtime.context_meter(sid)
            if meter is not None:
                self.emit(conn, {"context": meter})
            for record in records:
                self.emit(conn, {"record": record})
                after = record["seq"]
            settled = self.runtime.settled(sid)
            if settled is not None:
                self.emit(conn, {"settled": settled})
            # A watch that starts mid-reply gets the reply so far
            draft = self.runtime.current_draft(sid)
            if draft is not None:
                self.emit(conn, draft)
            while True:
                drafted = False
                for event in watcher.take(0.2):
                    record = event.get("record")
                    if record is not None:
                        if record["seq"] <= after:
                            continue
                        after = record["seq"]
                    drafted = drafted or "draft" in event
                    self.emit(conn, event)
                if drafted:
                    # At most DRAFT_EVERY_SECONDS a draft: the newest replaces what
                    # queues meanwhile (Watcher.put)
                    time.sleep(DRAFT_EVERY_SECONDS)
                if watcher.bye:
                    self.emit(conn, {"bye": watcher.bye})
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

    def watch_list(self, conn, request):
        """The list, then the whole list again whenever it changed, its changes
        gathered for a moment; it ends with a `bye` when the server stops."""
        try:
            archived, workspace = list_filters(request)
        except Refused as exc:
            self.emit(conn, error(str(exc)))
            return
        conn.settimeout(WATCH_SEND_SECONDS)
        sent = None
        version = self.runtime.list_version
        try:
            while True:
                sessions = self.runtime.sessions(archived, workspace)
                if sessions != sent:
                    self.emit(conn, {"sessions": sessions})
                    sent = sessions
                while True:
                    if self.stopping.is_set():
                        self.emit(conn, {"bye": self.reason})
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

    @staticmethod
    def emit(conn, event):
        conn.sendall((json.dumps(event, ensure_ascii=False) + "\n").encode())


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
