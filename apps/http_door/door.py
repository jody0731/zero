"""ZERO's HTTP door: one small process on 127.0.0.1 that serves the web UI and
ZERO over MCP, each request whole through ZERO's server's socket.

    GET  /                 the web UI's page (signed in; /?sign_in=CODE signs in)
    GET  /ui/NAME          its files (apps/web_ui/)
    POST /api/request      one of the protocol's messages, whole; the server's
                           reply as it came
    GET  /api/events       the page's one stream (Server-Sent Events): the
                           session list, and one session's events after a seq
    POST /mcp              MCP (apps/mcp/server.py), stateless; a tool call
                           answered on a stream with pings where the client
                           takes one
    GET, DELETE /mcp       405: no stream of the server's own, no sessions

Every request passes the guard first (guard.py). Each goes to ZERO's server
as one whole request, which starts the server where none answers, so the
server still leaves when idle; the page's watches are sent with
keeps_server_up false, so an open page doesn't hold it up: when it leaves,
the page is told it is asleep, and the door looks now and then for a server
again (any client's request starts one) to wake the page.

The system holds the door's port: systemd's socket unit hands the listening
socket over at the first knock (LISTEN_FDS); else the door binds it itself.
The door leaves after its idle seconds with no request in flight, no page's
stream open and no MCP call running; systemd starts it again at the next.
"""

import json
import os
import queue
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from apps.http_door.guard import Guard, Refused
from apps.mcp.server import McpServer, ZeroTools, answer_text
from client import launcher
from client.connection import (
    CONNECT_WAIT_SECONDS, FEATURES_WAIT_SECONDS, Connection, NotRunning,
    ProtocolError,
)
from protocol.messages import MESSAGES, SESSION_ID_ENV, request as build

# The web UI's files, served as they are
WEB_UI = Path(__file__).resolve().parents[1] / "web_ui"
FILE_TYPES = {".html": "text/html; charset=utf-8",
              ".css": "text/css; charset=utf-8",
              ".js": "text/javascript; charset=utf-8",
              ".svg": "image/svg+xml"}
# Minutes with nothing to do before the door leaves (the server's own
# default idle time)
IDLE_SECONDS = 300
# How often the door looks whether to leave
IDLE_LOOK_SECONDS = 1.0
# The largest request body taken (the server's own cap)
MAX_BODY = 1 << 20
# A stream's keepalive, under every client's idle read timeout
PING_SECONDS = 15
# While the server is asleep, how often an open page's stream looks for one
WAKE_LOOK_SECONDS = 2.0
# How long a request may wait on the server, by message; the rest the
# client's default
MESSAGE_SECONDS = {"model/features": FEATURES_WAIT_SECONDS,
                   "model/server/connect": CONNECT_WAIT_SECONDS,
                   "mcp/add": CONNECT_WAIT_SECONDS}
# What every response carries: the page loads only its own files, runs no
# inline script, is framed by no one, and sends no referrer
SECURITY_HEADERS = (
    ("Content-Security-Policy",
     "default-src 'self'; script-src 'self'; style-src 'self'; "
     "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
     "base-uri 'none'; form-action 'self'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
)
# Systemd's socket handover: the first socket passed is fd 3
LISTEN_FDS_START = 3
# Names in the session list a source takes
WEB_SOURCE = "web"
MCP_SOURCE = "mcp"

SIGNED_OUT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>ZERO</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/ui/style.css"></head>
<body class="signed-out"><main><h1>ZERO</h1>
<p>{why}</p></main></body></html>
"""


def handed_socket(environ=os.environ):
    """The listening socket systemd handed over (LISTEN_FDS), or None."""
    if environ.get("LISTEN_PID") != str(os.getpid()):
        return None
    try:
        count = int(environ.get("LISTEN_FDS", "0"))
    except ValueError:
        return None
    if count < 1:
        return None
    for name in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        os.environ.pop(name, None)
    return socket.socket(fileno=LISTEN_FDS_START)


def environ_session(pid):
    """The ZERO session a process is a command of, as ZERO set it in its
    environment ($ZERO_SESSION_ID), or None."""
    try:
        data = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    for entry in data.split(b"\0"):
        name, _, value = entry.partition(b"=")
        if name.decode(errors="replace") == SESSION_ID_ENV and value:
            return value.decode(errors="replace")
    return None


class Door(ThreadingHTTPServer):
    """The door's HTTP server: its guard, its count of what keeps it up, and
    how it reaches ZERO's server."""

    daemon_threads = True
    # Many clients at once: Claude Code's sessions, a page's requests
    request_queue_size = 64

    def __init__(self, paths, port, listener=None, idle_seconds=IDLE_SECONDS,
                 ensure=None, guard=None):
        self.paths = paths
        self.idle_seconds = idle_seconds
        self.ensure = ensure or (lambda: launcher.ensure_server(paths))
        self.mcp = McpServer(self.mcp_tools)
        self.busy_lock = threading.Lock()
        # Requests in flight and streams open: each keeps the door up
        self.busy = 0
        self.last_busy = time.monotonic()
        self.leaving = threading.Event()
        super().__init__(("127.0.0.1", port), Handler,
                         bind_and_activate=listener is None)
        if listener is not None:
            self.socket.close()
            self.socket = listener
        # The port it listens on (a test's 0 is the one the system picked)
        self.port = self.socket.getsockname()[1]
        self.guard = guard or Guard(self.port, paths.app_token,
                                    make_token=self.ensure)

    def handle_error(self, request, client_address):
        # A client that went mid-reply is no error of the door's
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)

    def server_bind(self):
        # Another door's port in use says so at once, not after a wait
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()

    def mcp_tools(self, headers):
        """The ZeroTools one MCP request is answered with: its client's
        process named to ZERO's server, a workflow's session read from it."""
        pid = getattr(headers, "client_pid", None)
        connection = Connection(self.paths, ensure=self.ensure,
                                source=MCP_SOURCE, relayed_for_pid=pid)
        return ZeroTools(connection, working_for=(
            (lambda: environ_session(pid)) if pid is not None
            else (lambda: None)))

    def connection(self, pid, source=WEB_SOURCE):
        return Connection(self.paths, ensure=self.ensure, source=source,
                          relayed_for_pid=pid)

    def hold(self):
        with self.busy_lock:
            self.busy += 1

    def let_go(self):
        with self.busy_lock:
            self.busy -= 1
            self.last_busy = time.monotonic()

    def watch_idle(self):
        """Leave once nothing has kept the door busy for its idle seconds."""
        while not self.leaving.wait(IDLE_LOOK_SECONDS):
            with self.busy_lock:
                quiet = time.monotonic() - self.last_busy
                if self.busy == 0 and quiet >= self.idle_seconds:
                    self.leaving.set()
        print(f"ZERO's HTTP door: idle for {self.idle_seconds:g} s, "
              "leaving.", flush=True)
        self.shutdown()

    def serve(self):
        """Serve until idle (or stopped)."""
        threading.Thread(target=self.watch_idle, daemon=True).start()
        try:
            self.serve_forever(poll_interval=0.2)
        finally:
            self.server_close()

    def stop(self):
        self.leaving.set()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ZERO"
    sys_version = ""
    # A keep-alive connection's own quiet time
    timeout = 120

    def setup(self):
        super().setup()
        # The kernel's word on this connection, asked once, when needed
        self.peer_found = None
        self.peer_asked = False

    def peer(self):
        if not self.peer_asked:
            self.peer_found = self.server.guard.peer_of(self.connection)
            self.peer_asked = True
        return self.peer_found

    def log_message(self, format, *args):
        # Each request a line on stderr (the unit's journal), its query left
        # out: a sign-in code is in one
        line = format % args
        print(f"ZERO's HTTP door: {line.split('?')[0]}", file=sys.stderr)

    # Every method goes through one place
    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")

    def do_OPTIONS(self):
        self.route("OPTIONS")

    def do_PUT(self):
        self.route("PUT")

    def route(self, method):
        self.server.hold()
        try:
            self.routed(method)
        except Refused as refusal:
            self.refuse(refusal.status, str(refusal))
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            self.server.let_go()

    def routed(self, method):
        url = urlsplit(self.path)
        path = url.path
        guard = self.server.guard
        if path == "/" and method == "GET":
            return self.page(parse_qs(url.query))
        if path.startswith("/ui/") and method == "GET":
            guard.place(self.headers)
            return self.file(path[len("/ui/"):])
        if path == "/mcp":
            if method != "POST":
                guard.place(self.headers)
                return self.refuse(405, "MCP here is POST only: no stream "
                                   "of the server's own, no sessions",
                                   allow="POST")
            return self.mcp_post(self.prove())
        if path == "/api/request" and method == "POST":
            return self.api_request(self.prove())
        if path == "/api/events" and method == "GET":
            return self.events(self.prove(), parse_qs(url.query))
        guard.place(self.headers)
        if path in ("/api/request", "/api/events"):
            return self.refuse(405, f"{path} takes "
                               f"{'POST' if path.endswith('request') else 'GET'}")
        return self.refuse(404, "nothing here")

    def prove(self):
        return self.server.guard.who(self.connection, self.headers,
                                     self.peer())

    # The page

    def page(self, query):
        guard = self.server.guard
        guard.place(self.headers)
        code = (query.get("sign_in") or [None])[0]
        if code is not None:
            value = guard.sign_in(code)
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", guard.set_cookie(value))
            self.send_header("Content-Length", "0")
            self.security_headers()
            self.end_headers()
            return
        try:
            self.prove()
        except Refused as refusal:
            body = SIGNED_OUT_PAGE.format(why=str(refusal)).encode()
            return self.send_body(401, body, FILE_TYPES[".html"])
        return self.file("index.html")

    def file(self, name):
        found = (WEB_UI / name).resolve()
        if (found.parent != WEB_UI.resolve() or found.suffix not in FILE_TYPES
                or not found.is_file()):
            return self.refuse(404, "nothing here")
        self.send_body(200, found.read_bytes(), FILE_TYPES[found.suffix],
                       cache="no-cache")

    # One message, whole

    def body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise Refused(400, "Content-Length is a number") from None
        if length > MAX_BODY:
            raise Refused(413, f"a request is at most {MAX_BODY} bytes")
        return self.rfile.read(length)

    def json_body(self):
        kind = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if kind.lower() != "application/json":
            raise Refused(415, "the body is JSON (Content-Type: "
                          "application/json)")
        try:
            return json.loads(self.body())
        except ValueError:
            raise Refused(400, "the body isn't JSON") from None

    def api_request(self, proof):
        payload = self.json_body()
        if not isinstance(payload, dict) or payload.get("message") not in MESSAGES:
            raise Refused(400, "a request is one of the protocol's messages: "
                          "{\"message\": NAME, ...}")
        name = payload["message"]
        if name == "session/watch" or (name == "session/list"
                                       and payload.get("watch")):
            raise Refused(400, "a watch goes through /api/events")
        # The token proves the door's client to the door; ZERO's server
        # takes the door's own word (its socket) and its client's process
        payload.pop("token", None)
        payload.pop("relayed_for_pid", None)
        connection = self.server.connection(proof.pid)
        try:
            reply = connection.reply_to(
                payload, MESSAGE_SECONDS.get(name, 30))
        except NotRunning as error:
            reply = {"ok": False, "error": str(error)}
        except ProtocolError as error:
            reply = {"ok": False, "error": str(error)}
        self.send_json(200, reply)

    # The page's stream

    def events(self, proof, query):
        session = (query.get("session") or [None])[0]
        try:
            after = int((query.get("after") or ["0"])[0])
        except ValueError:
            raise Refused(400, "after is a record's seq") from None
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.security_headers()
        self.end_headers()
        self.close_connection = True
        Stream(self, proof.pid, session, after).run()

    def write_event(self, kind, data):
        text = json.dumps(data, ensure_ascii=False)
        self.wfile.write(f"event: {kind}\ndata: {text}\n\n".encode())
        self.wfile.flush()

    def write_ping(self):
        self.wfile.write(b": ping\n\n")
        self.wfile.flush()

    # MCP

    def mcp_post(self, proof):
        text = self.body_for_mcp()
        headers = McpHeaders({name.lower(): value
                              for name, value in self.headers.items()},
                             proof.pid)
        streamed = "text/event-stream" in (headers.get("accept") or "")
        try:
            message = json.loads(text)
        except ValueError:
            message = None
        calls_a_tool = isinstance(message, dict) and (
            message.get("method") == "tools/call")
        if streamed and calls_a_tool:
            return self.mcp_streamed(text, headers)
        reply = answer_text(self.server.mcp, text, headers)
        if reply is None:
            # Notifications (and responses) only
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.security_headers()
            self.end_headers()
            return
        self.send_json(200, reply)

    def body_for_mcp(self):
        kind = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if kind.lower() != "application/json":
            raise Refused(415, "MCP's body is JSON (Content-Type: "
                          "application/json)")
        return self.body().decode("utf-8", errors="replace")

    def mcp_streamed(self, text, headers):
        """A tool call (zero_wait waits minutes) answered on a stream: its
        headers at once, a ping while it runs, then its reply."""
        replies = queue.Queue()
        threading.Thread(target=lambda: replies.put(answer_text(
            self.server.mcp, text, headers)), daemon=True).start()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        self.end_headers()
        self.close_connection = True
        while True:
            try:
                reply = replies.get(timeout=PING_SECONDS)
            except queue.Empty:
                self.write_ping()
                continue
            self.wfile.write(f"event: message\ndata: "
                             f"{json.dumps(reply, ensure_ascii=False)}\n\n"
                             .encode())
            self.wfile.flush()
            return

    # Replies

    def security_headers(self):
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)

    def send_body(self, status, body, kind, cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_body(status, body, "application/json")

    def refuse(self, status, why, allow=None):
        """A refusal, and the connection closed: a body it didn't read
        would be taken for the next request."""
        body = json.dumps({"ok": False, "error": why}).encode()
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        if allow:
            self.send_header("Allow", allow)
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)


class McpHeaders(dict):
    """An MCP request's headers by lowercase name, and its client's process
    (None where the kernel didn't say)."""

    def __init__(self, lower, client_pid):
        super().__init__(lower)
        self.client_pid = client_pid


class Stream:
    """One page's stream: the session list and, when the page shows one, a
    session's events after the seq it holds, each from a watch on ZERO's
    server that doesn't keep the server up. When the server leaves idle the
    page hears `asleep`; the stream then looks for a server now and then and
    says `awake` when one answers (any client's request starts one). Ends
    when the page goes."""

    def __init__(self, handler, pid, session, after):
        self.handler = handler
        self.paths = handler.server.paths
        self.connection = handler.server.connection(pid)
        self.session = session
        self.after = after
        self.events = queue.Queue()
        self.watches = []

    def run(self):
        write = self.handler.write_event
        try:
            awake = self.opened()
            if not awake:
                write("asleep", {"why": "no server"})
            while True:
                if not awake:
                    self.wait_for_server()
                    write("awake", {"after": self.after})
                awake = self.pump()
        except OSError:
            # The page went (its socket closed)
            return
        finally:
            self.close()

    def opened(self):
        """Whether the watches opened, each read in a thread into one
        queue; False where no server answers."""
        self.close()
        self.events = queue.Queue()
        try:
            watches = [("list", self.connection.stream(build(
                "session/list", watch=True, keeps_server_up=False)))]
            if self.session:
                watches.append(("session", self.connection.stream(build(
                    "session/watch", session=self.session, after=self.after,
                    keeps_server_up=False))))
        except (NotRunning, ProtocolError):
            self.close()
            return False
        self.watches = [watch for _, watch in watches]
        for kind, watch in watches:
            threading.Thread(target=self.read, args=(kind, watch, self.events),
                             daemon=True).start()
        return True

    @staticmethod
    def read(kind, watch, events):
        try:
            for event in watch:
                events.put((kind, event))
        except ProtocolError as error:
            # The server refused the watch (a session it doesn't have)
            events.put((kind, {"bye": "refused", "error": str(error)}))
            return
        except (OSError, ValueError):
            events.put((kind, {"bye": "lost"}))
            return
        events.put((kind, {"bye": watch.bye or "lost"}))

    def close(self):
        for watch in self.watches:
            watch.close()
        self.watches = []

    def pump(self):
        """The watches' events to the page until the server goes; False
        then (the page told it is asleep)."""
        write = self.handler.write_event
        while True:
            try:
                kind, event = self.events.get(timeout=PING_SECONDS)
            except queue.Empty:
                self.handler.write_ping()
                continue
            if "bye" not in event:
                record = event.get("record")
                if kind == "session" and record is not None:
                    self.after = max(self.after, record.get("seq", 0))
                write(kind, event)
                continue
            bye = event["bye"]
            # A session gone or refused: the page lets it go, the list
            # goes on
            if kind == "session" and bye in ("deleted", "refused"):
                write("session", event)
                self.session = None
                continue
            # A newer build, or a page that read too slowly: the watches
            # again from where the page is
            if bye in ("restart", "behind") and self.opened():
                continue
            self.close()
            write("asleep", {"why": bye})
            return False

    def wait_for_server(self):
        """Ping the page until a server answers and the watches open."""
        waited = 0.0
        while True:
            time.sleep(WAKE_LOOK_SECONDS)
            waited += WAKE_LOOK_SECONDS
            try:
                answering = launcher.hello(self.paths, timeout=1) is not None
            except NotRunning:
                answering = False
            if answering and self.opened():
                return
            if waited >= PING_SECONDS:
                waited = 0.0
                self.handler.write_ping()
