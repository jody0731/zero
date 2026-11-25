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

import base64
import json
import os
import queue
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from apps.http_door.guard import Guard, Refused
from apps.mcp.server import McpServer, ZeroTools, answer_text
from client import launcher
from client.connection import Connection, NotRunning, ProtocolError
from protocol.messages import MESSAGES, SESSION_ID_ENV, request as build

# The web UI's files, served as they are
WEB_UI = Path(__file__).resolve().parents[1] / "web_ui"
COMPONENTS_ROOT = Path(__file__).resolve().parents[2]
FILE_TYPES = {".html": "text/html; charset=utf-8",
              ".css": "text/css; charset=utf-8",
              ".js": "text/javascript; charset=utf-8",
              ".svg": "image/svg+xml"}
COMPONENT_FILE_TYPES = {".css": "text/css; charset=utf-8",
                        ".js": "text/javascript; charset=utf-8",
                        ".svg": "image/svg+xml"}
# Minutes with nothing to do before the door leaves (the server's own
# default idle time)
IDLE_SECONDS = 300
# How often the door looks whether to leave
IDLE_LOOK_SECONDS = 1.0
# The largest request body taken (the server's own cap)
MAX_BODY = 1 << 20
# Streaming attachment relay chunk size (fits MAX_BODY after base64 encoding)
ATTACHMENT_CHUNK_BYTES = 512 * 1024
# Strict allowlist of media types safe to display inline in a browser.
SAFE_INLINE_EXACT = frozenset({
    "image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf",
})
SAFE_INLINE_PREFIXES = ("audio/", "video/")
ATTACHMENT_CSP = "sandbox; default-src 'none'"
RANGE_RE = re.compile(r"^(\d*)-(\d*)$")
# A stream's keepalive, under every client's idle read timeout
PING_SECONDS = 15
# While the server is asleep, how often an open page's stream looks for one
WAKE_LOOK_SECONDS = 2.0
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

_MIME_TOKEN_RE = re.compile(
    r"^[a-z0-9!#$%&'*+\-.^_`|~]{1,127}/[a-z0-9!#$%&'*+\-.^_`|~]{1,127}$"
)


def sanitize_media_type(value, *, default="application/octet-stream"):
    """Validate strict RFC MIME type/subtype syntax; reject CR/LF and controls."""
    if not isinstance(value, str) or not value:
        return default
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return default
    essence = value.split(";", 1)[0].strip().lower()
    if not essence or _MIME_TOKEN_RE.fullmatch(essence) is None:
        return default
    return essence


def is_safe_inline_media(media_type):
    """True only for explicitly allowlisted browser-safe inline media types."""
    clean = (media_type or "").split(";")[0].strip().lower()
    if clean in SAFE_INLINE_EXACT:
        return True
    return any(clean.startswith(prefix) for prefix in SAFE_INLINE_PREFIXES)


def content_disposition(name, *, inline):
    """Build a safe Content-Disposition header with ASCII + RFC 5987 filename."""
    raw = (name or "file").replace("\\", "/").rsplit("/", 1)[-1]
    clean = "".join(ch for ch in raw if ord(ch) >= 32 and ord(ch) != 127) or "file"
    ascii_name = "".join(
        ch if 32 <= ord(ch) < 127 and ch not in ('"', "\\", ";") else "_"
        for ch in clean
    ) or "file"
    kind = "inline" if inline else "attachment"
    encoded = quote(clean, safe="")
    return f'{kind}; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded}'


def parse_byte_range(header_value, total_size):
    """Parse one HTTP byte Range header into `(start, end)` inclusive, or None.

    Returns `None` for non-bytes units or comma-separated multi-range headers
    (serving 200 OK full representation per RFC 9110), and `"invalid"` for
    malformed or unsatisfiable single byte ranges (serving 416).
    """
    if not header_value:
        return None
    spec = header_value.strip()
    unit, sep, rest = spec.partition("=")
    if not sep:
        return "invalid"
    if unit.strip().lower() != "bytes":
        return None
    if "," in rest:
        return None
    match = RANGE_RE.fullmatch(rest.strip())
    if not match:
        return "invalid"
    start_s, end_s = match.groups()
    if total_size <= 0 or (not start_s and not end_s):
        return "invalid"
    if not start_s:
        suffix = int(end_s)
        if suffix <= 0:
            return "invalid"
        start = max(0, total_size - suffix)
        end = total_size - 1
        return start, end
    start = int(start_s)
    end = int(end_s) if end_s else total_size - 1
    if start >= total_size or start > end:
        return "invalid"
    return start, min(end, total_size - 1)


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


def component_ui_roots(root=None):
    """Return {name: web_ui_dir} for every component with web_ui/ui.js."""
    parent = (Path(root) if root is not None else COMPONENTS_ROOT) / "components"
    if not parent.is_dir():
        return {}
    roots = {}
    for folder in sorted(parent.iterdir()):
        if not folder.is_dir() or folder.name.startswith((".", "_")):
            continue
        ui_dir = folder / "web_ui"
        if ui_dir.is_dir() and (ui_dir / "ui.js").is_file():
            roots[folder.name] = ui_dir.resolve()
    return roots


def discover_web_ui(root=None):
    """Return JSON descriptors for discovered component web UI bundles."""
    found = []
    for name, ui_dir in component_ui_roots(root).items():
        has_style = (ui_dir / "style.css").is_file()
        found.append({
            "name": name,
            "module": f"/ui/components/{name}/ui.js",
            "style": f"/ui/components/{name}/style.css" if has_style else None,
        })
    return found


class Door(ThreadingHTTPServer):
    """The door's HTTP server: its guard, its count of what keeps it up, and
    how it reaches ZERO's server."""

    daemon_threads = True
    # Many clients at once: Claude Code's sessions, a page's requests
    request_queue_size = 64

    def __init__(self, paths, port, listener=None, idle_seconds=IDLE_SECONDS,
                 ensure=None, guard=None, components_root=None):
        self.paths = paths
        self.idle_seconds = idle_seconds
        self.components_root = (
            Path(components_root) if components_root is not None
            else COMPONENTS_ROOT
        )
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
        connection = Connection(
            self.paths, ensure=self.ensure, source=MCP_SOURCE,
            relayed_for_pid=pid, relayed=True,
        )
        return ZeroTools(connection, working_for=(
            (lambda: environ_session(pid)) if pid is not None
            else (lambda: None)))

    def connection(self, pid, source=WEB_SOURCE):
        return Connection(
            self.paths, ensure=self.ensure, source=source,
            relayed_for_pid=pid, relayed=True,
        )

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

    def do_HEAD(self):
        self.route("HEAD")

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
        if path == "/ui/components.json" and method == "GET":
            guard.place(self.headers)
            return self.send_json(
                200, discover_web_ui(self.server.components_root),
            )
        if path.startswith("/ui/components/") and method == "GET":
            guard.place(self.headers)
            return self.component_file(path[len("/ui/components/"):])
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
        if path == "/api/browser" and method == "POST":
            guard.place(self.headers)
            if self.headers.get("Origin") not in guard.origins:
                raise Refused(403, "browser pairing needs this page's origin")
            payload = self.json_body()
            if not isinstance(payload, dict):
                raise Refused(400, "browser pairing is a JSON object")
            value = guard.restore_browser(payload.get("key"))
            self.send_response(204)
            self.send_header("Set-Cookie", guard.set_cookie(value))
            self.send_header("Content-Length", "0")
            self.security_headers()
            self.end_headers()
            return
        if path == "/api/request" and method == "POST":
            return self.api_request(self.prove())
        if path == "/api/attachments" and method == "POST":
            return self.upload_attachment(self.prove(), parse_qs(url.query))
        if path.startswith("/api/attachments/") and method in ("GET", "HEAD"):
            aid = unquote(path[len("/api/attachments/"):])
            return self.get_attachment(
                self.prove(), aid, parse_qs(url.query), head_only=method == "HEAD",
            )
        if path == "/api/dictation" and method == "POST":
            return self.dictation_start(self.prove())
        if path.startswith("/api/dictation/"):
            tail = path[len("/api/dictation/"):]
            return self.dictation_route(
                self.prove(), method, tail, parse_qs(url.query),
            )
        if path == "/api/speech" and method == "POST":
            return self.speech_synthesize(self.prove())
        if path == "/api/events" and method == "GET":
            return self.events(self.prove(), parse_qs(url.query))
        guard.place(self.headers)
        if path in (
            "/api/request", "/api/events", "/api/attachments",
            "/api/dictation", "/api/speech",
        ):
            want = "GET" if path.endswith("events") else "POST"
            return self.refuse(405, f"{path} takes {want}", allow=want)
        if path.startswith("/api/attachments/"):
            return self.refuse(405, f"{path} takes GET", allow="GET, HEAD")
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
            launch = {name: values[0] for name, values in query.items()
                      if name in ("workspace", "session", "model", "mode",
                                  "thinking_effort", "view") and values}
            launch["pair"] = guard.browser_key()
            self.send_header("Location", "/#" + urlencode(launch))
            self.send_header("Set-Cookie", guard.set_cookie(value))
            self.send_header("Content-Length", "0")
            self.security_headers()
            self.end_headers()
            return
        try:
            self.prove()
        except Refused as refusal:
            if refusal.status != 401:
                raise
            # The public shell restores an already-paired browser. No
            # session data is returned before the cookie has been restored.
        return self.file("index.html")

    def file(self, name):
        root = WEB_UI.resolve()
        found = (WEB_UI / name).resolve()
        if (not found.is_relative_to(root) or found.suffix not in FILE_TYPES
                or not found.is_file()):
            return self.refuse(404, "nothing here")
        self.send_body(200, found.read_bytes(), FILE_TYPES[found.suffix],
                       cache="no-cache")

    def component_file(self, tail):
        name, sep, relpath = tail.partition("/")
        if not name or not sep or not relpath or name in (".", ".."):
            return self.refuse(404, "nothing here")
        ui_dir = component_ui_roots(self.server.components_root).get(name)
        if ui_dir is None:
            return self.refuse(404, "nothing here")
        found = (ui_dir / relpath).resolve()
        if (not found.is_relative_to(ui_dir)
                or found.suffix not in COMPONENT_FILE_TYPES
                or not found.is_file()):
            return self.refuse(404, "nothing here")
        self.send_body(
            200, found.read_bytes(), COMPONENT_FILE_TYPES[found.suffix],
            cache="no-cache",
        )

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
        if name == "dictation/watch":
            raise Refused(
                400, "dictation streams go through /api/dictation/<id>/events",
            )
        if name == "attachment/add":
            raise Refused(
                403,
                "attachment/add reads a local host path and is not permitted "
                "over HTTP; upload bytes through POST /api/attachments",
            )
        if name == "media/transcribe" and payload.get("path"):
            raise Refused(
                403,
                "media/transcribe with a local host path is not permitted over HTTP",
            )
        # The token proves the door's client to the door; ZERO's server
        # takes the door's own word (its socket) and its client's process
        payload.pop("token", None)
        payload.pop("relayed_for_pid", None)
        payload.pop("relayed", None)
        connection = self.server.connection(proof.pid)
        if "source" in MESSAGES[name]["fields"]:
            payload["source"] = WEB_SOURCE
        try:
            reply = connection.reply_to(
                payload, MESSAGES[name].get("timeout_seconds", 30))
        except NotRunning as error:
            reply = {"ok": False, "error": str(error)}
        except ProtocolError as error:
            reply = {"ok": False, "error": str(error)}
        self.send_json(200, reply)

    # Streaming attachment relay

    def _stream_request_chunks(self):
        """Yield raw HTTP request body slices (<= 512 KiB) without buffering."""
        transfer = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer:
            while True:
                line = self.rfile.readline(65536)
                if not line:
                    raise Refused(400, "unexpected end of chunked upload")
                size_text = line.split(b";", 1)[0].strip()
                try:
                    chunk_size = int(size_text, 16)
                except ValueError:
                    raise Refused(400, "invalid chunked encoding") from None
                if chunk_size == 0:
                    while True:
                        trailer = self.rfile.readline(65536)
                        if not trailer or trailer in (b"\r\n", b"\n"):
                            break
                    return
                remaining = chunk_size
                while remaining > 0:
                    piece = self.rfile.read(min(ATTACHMENT_CHUNK_BYTES, remaining))
                    if not piece:
                        raise Refused(400, "unexpected end of chunked upload")
                    remaining -= len(piece)
                    yield piece
                crlf = self.rfile.read(2)
                if crlf != b"\r\n":
                    raise Refused(400, "invalid chunk terminator")
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            raise Refused(411, "Content-Length is required for attachment upload")
        try:
            remaining = int(raw_len)
        except ValueError:
            raise Refused(400, "Content-Length is a number") from None
        if remaining < 0:
            raise Refused(400, "Content-Length must be nonnegative")
        while remaining > 0:
            piece = self.rfile.read(min(ATTACHMENT_CHUNK_BYTES, remaining))
            if not piece:
                raise Refused(400, "upload body ended before Content-Length")
            remaining -= len(piece)
            yield piece

    def upload_attachment(self, proof, query):
        """POST /api/attachments: stream file bytes in <= 512 KiB chunks."""
        name = (
            (query.get("name") or [None])[0]
            or unquote(self.headers.get("X-Attachment-Name") or "")
            or "file"
        )
        media_hint = (
            (query.get("media_type") or [None])[0]
            or (self.headers.get("Content-Type") or "").split(";")[0].strip()
            or None
        )
        raw_override = (
            (query.get("allow_over_limit") or [None])[0]
            or self.headers.get("X-Allow-Over-Limit")
            or ""
        ).strip().lower()
        allow_over_limit = raw_override in ("1", "true", "yes")
        expected_size = None
        if "Content-Length" in self.headers:
            try:
                expected_size = int(self.headers["Content-Length"])
            except ValueError:
                raise Refused(400, "Content-Length is a number") from None
            if expected_size < 0:
                raise Refused(400, "Content-Length must be nonnegative")
        connection = self.server.connection(proof.pid)
        try:
            attachment = connection.upload_attachment_chunks(
                name,
                self._stream_request_chunks(),
                expected_size=expected_size,
                media_type=media_hint,
                allow_over_limit=allow_over_limit,
                chunk_bytes=ATTACHMENT_CHUNK_BYTES,
            )
        except ProtocolError as error:
            self.close_connection = True
            status = 413 if error.over_limit else 400
            payload = {"ok": False, "error": str(error)}
            if error.over_limit:
                payload["over_limit"] = True
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.security_headers()
            self.end_headers()
            self.wfile.write(body)
            return
        except NotRunning as error:
            self.close_connection = True
            return self.refuse(503, str(error))
        self.send_json(200, {"ok": True, "attachment": attachment})

    def get_attachment(self, proof, attachment_id, query, *, head_only=False):
        """GET/HEAD /api/attachments/<id>: stream with Range 206 & XSS hygiene."""
        if not attachment_id or "/" in attachment_id:
            return self.refuse(404, "nothing here")
        session = (query.get("session") or [None])[0] or None
        force_download = (
            (query.get("download") or [""])[0].strip().lower()
            in ("1", "true", "yes")
        )
        connection = self.server.connection(proof.pid)
        try:
            meta = connection.get_attachment(attachment_id, session=session)
        except ProtocolError as error:
            return self.refuse(404, str(error))
        except NotRunning as error:
            return self.refuse(503, str(error))
        total = int(meta.get("size_bytes") or 0)
        media_type = sanitize_media_type(meta.get("media_type"))
        inline = is_safe_inline_media(media_type) and not force_download
        range_header = self.headers.get("Range")
        byte_range = parse_byte_range(range_header, total)
        if byte_range == "invalid":
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.send_header("Content-Length", "0")
            self.send_header("Accept-Ranges", "bytes")
            self.security_headers()
            self.end_headers()
            return
        if byte_range is not None:
            start, end = byte_range
            status = 206
            length = end - start + 1
        else:
            start = 0
            end = total - 1 if total > 0 else -1
            status = 200
            length = total
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header(
            "Content-Disposition",
            content_disposition(meta.get("name"), inline=inline),
        )
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        if inline:
            self.security_headers()
        else:
            self.send_header("Content-Security-Policy", ATTACHMENT_CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()
        if head_only or length <= 0:
            return
        try:
            for chunk in connection.iter_attachment(
                attachment_id,
                session=session,
                start=start,
                end=end,
                chunk_bytes=ATTACHMENT_CHUNK_BYTES,
            ):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (NotRunning, ProtocolError):
            self.close_connection = True

    # Dictation and speech relays

    def _relay_media(self, proof, message_name, fields):
        connection = self.server.connection(proof.pid)
        payload = build(message_name, **fields)
        if "source" in MESSAGES[message_name]["fields"]:
            payload["source"] = WEB_SOURCE
        try:
            reply = connection.reply_to(
                payload, MESSAGES[message_name].get("timeout_seconds", 60),
            )
        except (NotRunning, ProtocolError) as error:
            reply = {"ok": False, "error": str(error)}
        status = 200 if reply.get("ok") else 400
        self.send_json(status, reply)

    def dictation_start(self, proof):
        payload = self.json_body() if int(
            self.headers.get("Content-Length", "0") or "0"
        ) > 0 else {}
        if not isinstance(payload, dict):
            raise Refused(400, "dictation start expects a JSON object")
        allowed = ("session", "model", "stt_service", "language", "mode")
        fields = {
            k: payload[k]
            for k in allowed
            if k in payload and payload[k] is not None
        }
        self._relay_media(proof, "dictation/start", fields)

    def dictation_route(self, proof, method, tail, query):
        parts = tail.split("/")
        if len(parts) != 2 or not parts[0]:
            return self.refuse(404, "nothing here")
        did = unquote(parts[0])
        action = parts[1]
        if action == "chunk":
            if method != "POST":
                return self.refuse(405, "chunk takes POST", allow="POST")
            raw_ctype = self.headers.get("Content-Type") or ""
            ctype = raw_ctype.split(";")[0].strip().lower()
            if ctype == "application/json":
                payload = self.json_body()
                if not isinstance(payload, dict):
                    raise Refused(400, "dictation chunk expects a JSON object")
                fields = {
                    "dictation_id": did,
                    "seq": payload.get("seq"),
                    "data": payload.get("data"),
                }
                if payload.get("sample_rate") is not None:
                    fields["sample_rate"] = payload["sample_rate"]
            else:
                raw_bytes = self.body()
                try:
                    seq = int((query.get("seq") or [""])[0])
                except ValueError:
                    raise Refused(400, "seq must be an integer") from None
                fields = {
                    "dictation_id": did,
                    "seq": seq,
                    "data": base64.b64encode(raw_bytes).decode("ascii"),
                }
                if raw_rate := (query.get("sample_rate") or [None])[0]:
                    try:
                        fields["sample_rate"] = int(raw_rate)
                    except ValueError:
                        raise Refused(400, "sample_rate must be an integer") from None
            return self._relay_media(proof, "dictation/chunk", fields)
        if action == "stop":
            if method != "POST":
                return self.refuse(405, "stop takes POST", allow="POST")
            return self._relay_media(proof, "dictation/stop", {"dictation_id": did})
        if action == "cancel":
            if method != "POST":
                return self.refuse(405, "cancel takes POST", allow="POST")
            return self._relay_media(proof, "dictation/cancel", {"dictation_id": did})
        if action == "events":
            if method != "GET":
                return self.refuse(405, "events takes GET", allow="GET")
            return self.dictation_events(proof, did)
        return self.refuse(404, "nothing here")

    def dictation_events(self, proof, dictation_id):
        connection = self.server.connection(proof.pid)
        try:
            watch = connection.watch_dictation(dictation_id, keeps_server_up=False)
        except (NotRunning, ProtocolError) as error:
            return self.refuse(400, str(error))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.security_headers()
        self.end_headers()
        self.close_connection = True
        events_box = queue.Queue()

        def reader():
            try:
                for ev in watch:
                    events_box.put(ev)
                events_box.put({"event": "done", "bye": watch.bye or "done"})
            except ProtocolError as error:
                events_box.put({"event": "error", "error": str(error), "bye": "error"})
            except (OSError, ValueError):
                events_box.put({"event": "done", "bye": "lost"})

        threading.Thread(target=reader, daemon=True).start()
        try:
            while True:
                try:
                    ev = events_box.get(timeout=PING_SECONDS)
                except queue.Empty:
                    self.write_ping()
                    continue
                kind = ev.get("event") or "message"
                self.write_event(kind, ev)
                if "bye" in ev or kind == "done":
                    return
        except OSError:
            return
        finally:
            watch.close()

    def speech_synthesize(self, proof):
        payload = self.json_body()
        if not isinstance(payload, dict):
            raise Refused(400, "speech request expects a JSON object")
        allowed = ("session", "reply_entry_id", "text", "voice", "service", "model")
        fields = {
            k: payload[k]
            for k in allowed
            if k in payload and payload[k] is not None
        }
        self._relay_media(proof, "speech/synthesize", fields)

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
                "session/list", watch=True, archived=None, keeps_server_up=False)))]
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
