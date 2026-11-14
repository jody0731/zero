"""ZERO as an MCP server, so another agent (Claude Code, Codex, any MCP client)
hands it work: on ZERO's HTTP door at /mcp (apps/http_door/), which one URL
gives every session of every agent, and on stdio for an app that speaks only
that (`zero mcp serve`).

Four tools, each one or two of the protocol's messages through the same
client every face uses:

    zero_start(task, workspace, mode, model)  session/send → the session's id
    zero_wait(session, seconds)               session/watch until it settles
                                              → the result object
    zero_reply(session, text, choice)         session/send with reply_to
    zero_stop(session)                        session/stop

Who answers ZERO's asks is the mode the caller starts the work in, which
zero_start's reply names with its line (the server's): the caller, with
zero_reply (its yes, or one of the choices the ask lists, approves), or
ZERO's judge, and then zero_wait returns only when the work is done or
stuck. It names no mode and no choice of its own: the server sends them.
On stdio the server is the caller's client, a process of the caller's and
not of ZERO's; on the door, each call names the caller's process to ZERO's
server (relayed_for_pid), so the server weighs the caller, not the door. Run
as one of a ZERO session's commands (a workflow, $ZERO_SESSION_ID set),
zero_start hands the task to a subagent of that session, as `zero -p` does
there.
"""

import json
import sys
import threading
import time
from pathlib import Path

from client.connection import (
    Connection, NotRunning, ProtocolError, command_session,
)
from client.launcher import ensure_server
from client.result import Result, latest_turn
from paths import Paths, changelog_version

SOURCE = "mcp"
# What a tool says back as its error, rather than a crash
ERRORS = (ValueError, LookupError, ProtocolError, NotRunning)
# How long zero_wait waits by default, and at most
WAIT_SECONDS = 300
MAX_WAIT_SECONDS = 3600
# Seconds before a dropped watch is followed again
RECONNECT_SECONDS = 0.2

INSTRUCTIONS = (
    "Z.E.R.O. is a general purpose agent on this machine with its own tools "
    "(shell, files, web), sandbox and background jobs. zero_start hands it a "
    "task in a folder and returns a session id at once; zero_wait returns "
    "the result when the session settles (outcome answered, waiting for an "
    "answer to a question, stopped or failed) or after its seconds with "
    "outcome running; zero_reply answers a waiting question or sends a "
    "follow-up; zero_stop stops the running turn. The mode decides who "
    "answers ZERO's questions, the caller with zero_reply or ZERO's own "
    "judge; zero_start's reply says which."
)


class ZeroTools:
    """The four tools as plain methods over a Connection, for any face.
    `working_for()`: the ZERO session whose command the caller is, or None
    (on stdio this process's own, $ZERO_SESSION_ID; on the HTTP door its
    client's, which the door reads from the kernel)."""

    def __init__(self, connection, working_for=command_session):
        self.connection = connection
        self.working_for = working_for

    def session(self, sid):
        """A session's row by its id or the start of it."""
        rows = self.connection.sessions()
        matches = [r for r in rows if r["id"] == sid] or [
            r for r in rows if r["id"].startswith(sid)]
        if len(matches) != 1:
            found = "no session" if not matches else f"{len(matches)} sessions"
            raise LookupError(f"{found} matching {sid!r}")
        return matches[0]

    def start(self, task, workspace, mode=None, model=None):
        folder = Path(workspace).expanduser()
        if not folder.is_absolute() or not folder.is_dir():
            raise ValueError(f"the workspace is a folder's absolute path; "
                             f"{workspace!r} isn't one")
        working_for = self.working_for()
        if working_for is not None:
            # A workflow's call: a subagent of the session whose command
            # this is, in its workspace, mode and model
            reply = self.connection.send_for(task, working_for)
        else:
            reply = self.connection.send(task, workspace=str(folder.resolve()),
                                         mode=mode, model=model)
        return {"ok": True, "session": reply["session"],
                **self.mode_of(reply["session"])}

    def mode_of(self, sid):
        """The session's mode and its line, as the server says it."""
        mode = self.session(sid)["mode"]
        rows = self.connection.hello().get("modes") or ()
        about = next((row.get("about", "") for row in rows
                      if isinstance(row, dict) and row.get("name") == mode), "")
        return {"mode": mode, "mode_about": about}

    def wait(self, session, seconds=WAIT_SECONDS):
        """The session's result once it settles, or "running" when the
        seconds run out. A watch that drops first (the server restarting
        for a newer build, say) is followed again, its turn read afresh."""
        sid = self.session(session)["id"]
        seconds = max(1, min(int(seconds), MAX_WAIT_SECONDS))
        deadline = time.monotonic() + seconds
        while True:
            records, _ = self.connection.history(sid, limit=None)
            result = Result(sid)
            stream = self.connection.watch(sid, after=latest_turn(records))
            done = threading.Event()

            def read(stream=stream, result=result, done=done):
                try:
                    for event in stream:
                        if result.add(event):
                            break
                except (OSError, ValueError):
                    pass
                finally:
                    done.set()

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            done.wait(max(0.0, deadline - time.monotonic()))
            stream.close()
            # Read once the reader is done with it
            reader.join(1)
            if result.settled or time.monotonic() >= deadline:
                return result.as_json()
            time.sleep(min(RECONNECT_SECONDS,
                           max(0.0, deadline - time.monotonic())))

    def reply(self, session, text=None, choice=None):
        info = self.session(session)
        ask = info.get("ask")
        if choice is not None and ask is None:
            raise ValueError("no question waits in this session")
        if choice is None and not text:
            raise ValueError("a reply takes text, or a choice")
        sent = self.connection.send(
            text or choice, session=info["id"],
            reply_to=None if ask is None else ask["id"], choice=choice,
        )
        return {"ok": True, "session": info["id"],
                "handled_as": sent.get("handled_as")}

    def stop(self, session):
        reply = self.connection.stop(self.session(session)["id"])
        return {"ok": True, "stopped": reply.get("stopped", False)}




# MCP's versions this server answers. The handshake's (`initialize`, then
# requests on its terms), newest last; then the stateless one's, where each
# request is whole: its version and the client's capabilities in its own
# `params._meta`, mirrored in headers over HTTP (2026-07-28)
HANDSHAKE_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
STATELESS_VERSIONS = ("2026-07-28",)
# A stateless request's `_meta` keys, and its headers over HTTP
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"
VERSION_HEADER = "mcp-protocol-version"
METHOD_HEADER = "mcp-method"
NAME_HEADER = "mcp-name"
# JSON-RPC's error codes, and MCP's own
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022
# The first version that carries a tool's result as data too
STRUCTURED_FROM = "2025-06-18"


def optional(kind, title):
    """An argument that may be left out (null)."""
    return {"anyOf": [{"type": kind}, {"type": "null"}], "default": None,
            "title": title}


def arguments_schema(tool, properties, required):
    return {"type": "object", "properties": properties,
            "required": list(required), "title": f"{tool}Arguments"}


def result_schema(tool):
    return {"additionalProperties": True, "title": f"{tool}DictOutput",
            "type": "object"}


# The four tools as another agent's model reads them: names, descriptions
# and schemas as `zero mcp serve` gave them up to 7.6.1 (from the MCP
# library's reading of their Python signatures), unchanged
TOOLS = (
    {"name": "zero_start", "description": (
        "Hand ZERO a task: a new session in `workspace` (a folder's absolute "
        "path), started at once; returns its id, its mode and the mode's "
        "line (its sandbox, and who answers ZERO's questions: you with "
        "zero_reply, or ZERO's judge). `mode`: one of ZERO's modes by name "
        "(a name it doesn't know is refused with the list); left out, the "
        "user's default. `model`: left out, the user's default."),
     "inputSchema": arguments_schema("zero_start", {
         "task": {"title": "Task", "type": "string"},
         "workspace": {"title": "Workspace", "type": "string"},
         "mode": optional("string", "Mode"),
         "model": optional("string", "Model"),
     }, ("task", "workspace")),
     "outputSchema": result_schema("zero_start")},
    {"name": "zero_wait", "description": (
        "Wait for a ZERO session to settle, up to `seconds` (300; 3600 at "
        "most). Returns session, outcome (answered, waiting: a question "
        "waits in `ask`, stopped, failed, or running when the seconds ran "
        "out), answer, ask, tokens and seconds."),
     "inputSchema": arguments_schema("zero_wait", {
         "session": {"title": "Session", "type": "string"},
         "seconds": {"default": WAIT_SECONDS, "title": "Seconds",
                     "type": "integer"},
     }, ("session",)),
     "outputSchema": result_schema("zero_wait")},
    {"name": "zero_reply", "description": (
        "Answer the question waiting in a ZERO session, in words (`text`) or "
        "with one of the choices its ask lists (`choices`, each with its "
        "words in `choice_labels`). With no question waiting, `text` is a "
        "follow-up message. Then zero_wait."),
     "inputSchema": arguments_schema("zero_reply", {
         "session": {"title": "Session", "type": "string"},
         "text": optional("string", "Text"),
         "choice": optional("string", "Choice"),
     }, ("session",)),
     "outputSchema": result_schema("zero_reply")},
    {"name": "zero_stop", "description": "Stop a ZERO session's running turn.",
     "inputSchema": arguments_schema("zero_stop", {
         "session": {"title": "Session", "type": "string"},
     }, ("session",)),
     "outputSchema": result_schema("zero_stop")},
)
TOOL_NAMES = tuple(tool["name"] for tool in TOOLS)


class McpError(Exception):
    """A JSON-RPC error: its code, its message, and data where it has some."""

    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


def is_notification(message):
    return isinstance(message, dict) and "id" not in message


class McpServer:
    """MCP's requests answered with the four tools, whatever carries them
    (stdio, HTTP). Stateless: each request is answered on its own, so a
    handshake leaves nothing behind and one server answers every client.
    `tools(request_headers)`: the ZeroTools a request is answered with."""

    def __init__(self, tools, release=None):
        self.tools = tools
        self.release = release or changelog_version() or "unknown"

    def server_info(self):
        return {"name": "zero", "title": "Z.E.R.O.", "version": self.release}

    def capabilities(self):
        return {"tools": {"listChanged": False}}

    def answer(self, message, headers=None):
        """One JSON-RPC message's reply, or None for a notification (or a
        client's response, which this server never asks for). `headers`:
        the HTTP request's, by lowercase name; None on stdio."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return failure(None, McpError(INVALID_REQUEST,
                                          "not a JSON-RPC 2.0 message"))
        if "method" not in message:
            # A response to a request of ours: there are none
            return None
        msg_id = message.get("id")
        if is_notification(message):
            return None
        try:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "result": self.result(message, headers)}
        except McpError as error:
            return failure(msg_id, error)
        except Exception as error:
            print(f"ZERO's MCP server: {type(error).__name__}: {error}",
                  file=sys.stderr)
            return failure(msg_id, McpError(INTERNAL_ERROR, "internal error"))

    def result(self, message, headers):
        method = message.get("method")
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise McpError(INVALID_PARAMS, "params is an object")
        meta = params.get("_meta")
        if method == "initialize":
            return self.initialized(params)
        stateless = isinstance(meta, dict) and VERSION_KEY in meta
        version = None
        if stateless or method == "server/discover":
            version = self.stateless_version(message, params, headers)
        if method == "server/discover":
            return {"supportedVersions": [*STATELESS_VERSIONS],
                    "capabilities": self.capabilities(),
                    "instructions": INSTRUCTIONS, "ttlMs": 0,
                    "cacheScope": "private", "resultType": "complete",
                    "_meta": {SERVER_INFO_KEY: self.server_info()}}
        if method == "ping":
            return {}
        if method == "tools/list":
            listed = {"tools": [dict(tool) for tool in TOOLS]}
            if version is not None:
                listed.update(ttlMs=0, cacheScope="private",
                              resultType="complete")
            return listed
        if method == "tools/call":
            return self.called(params, headers, version)
        raise McpError(METHOD_NOT_FOUND, f"method not found: {method}")

    def initialized(self, params):
        """The handshake's answer: the client's version where this server
        answers it, else its newest (the client then decides)."""
        asked = params.get("protocolVersion")
        version = (asked if asked in HANDSHAKE_VERSIONS
                   else HANDSHAKE_VERSIONS[-1])
        return {"protocolVersion": version,
                "capabilities": self.capabilities(),
                "serverInfo": self.server_info(),
                "instructions": INSTRUCTIONS}

    def stateless_version(self, message, params, headers):
        """A whole request's version, its envelope and headers checked in
        the order MCP gives (2026-07-28): the envelope's keys, then the
        headers against the body, then the version."""
        meta = params.get("_meta")
        if not isinstance(meta, dict):
            raise McpError(INVALID_PARAMS, "params._meta must be an object "
                           f"carrying {VERSION_KEY!r} and {CAPABILITIES_KEY!r}")
        missing = [key for key in (VERSION_KEY, CAPABILITIES_KEY)
                   if key not in meta]
        if missing:
            raise McpError(INVALID_PARAMS, "params._meta is missing "
                           + ", ".join(missing))
        version = meta[VERSION_KEY]
        if headers is not None:
            method = message.get("method")
            if headers.get(VERSION_HEADER) != version:
                raise McpError(HEADER_MISMATCH, f"{VERSION_HEADER} doesn't "
                               "match the request's protocol version")
            if headers.get(METHOD_HEADER) != method:
                raise McpError(HEADER_MISMATCH, f"{METHOD_HEADER} doesn't "
                               "match the request's method")
            name = params.get("name")
            if method == "tools/call" and name is not None and (
                    headers.get(NAME_HEADER) != name):
                raise McpError(HEADER_MISMATCH, f"{NAME_HEADER} doesn't "
                               "match the tool's name")
        if not isinstance(version, str):
            raise McpError(INVALID_PARAMS, "the protocol version is a string")
        if version not in STATELESS_VERSIONS:
            raise McpError(UNSUPPORTED_VERSION,
                           f"unsupported protocol version {version}",
                           {"supported": [*STATELESS_VERSIONS],
                            "requested": version})
        return version

    def called(self, params, headers, version):
        """A tool's result: its reply as text, and as data from 2025-06-18
        on; a tool's error as an error result the calling model reads."""
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in TOOL_NAMES:
            raise McpError(INVALID_PARAMS, f"unknown tool: {name}")
        if not isinstance(arguments, dict):
            raise McpError(INVALID_PARAMS, "a tool's arguments are an object")
        tools = self.tools(headers)
        try:
            reply = call_tool(tools, name, arguments)
        except ERRORS as error:
            done = {"content": [{"type": "text", "text": str(error)}],
                    "isError": True}
        else:
            done = {"content": [{"type": "text",
                                 "text": json.dumps(reply, indent=2)}],
                    "structuredContent": reply, "isError": False}
        if version is not None:
            done["resultType"] = "complete"
        return done


def call_tool(tools, name, arguments):
    """One of the four tools, its arguments checked as its schema says."""
    schema = next(tool for tool in TOOLS if tool["name"] == name)["inputSchema"]
    for field in schema["required"]:
        if field not in arguments:
            raise ValueError(f"{name} needs {field}")
    unknown = set(arguments) - set(schema["properties"])
    if unknown:
        raise ValueError(f"{name} takes no {', '.join(sorted(unknown))}")
    for field, value in arguments.items():
        spec = schema["properties"][field]
        kinds = [spec["type"]] if "type" in spec else [
            each["type"] for each in spec["anyOf"]]
        if not any(fits(value, kind) for kind in kinds):
            raise ValueError(f"{name}'s {field} is {' or '.join(kinds)}")
    given = {field: value for field, value in arguments.items()}
    if name == "zero_start":
        return tools.start(**given)
    if name == "zero_wait":
        return tools.wait(**given)
    if name == "zero_reply":
        return tools.reply(**given)
    return tools.stop(**given)


def fits(value, kind):
    if kind == "null":
        return value is None
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return type(value) is int
    return False


def failure(msg_id, error):
    body = {"code": error.code, "message": str(error)}
    if error.data is not None:
        body["data"] = error.data
    return {"jsonrpc": "2.0", "id": msg_id, "error": body}


def answer_text(server, text, headers=None):
    """The reply to a JSON-RPC message or batch as it came in text, or None
    when nothing is owed (notifications only)."""
    try:
        message = json.loads(text)
    except ValueError:
        return failure(None, McpError(PARSE_ERROR, "invalid JSON"))
    if isinstance(message, list):
        if not message:
            return failure(None, McpError(INVALID_REQUEST, "an empty batch"))
        replies = [server.answer(each, headers) for each in message]
        replies = [reply for reply in replies if reply is not None]
        return replies or None
    return server.answer(message, headers)


def serve():
    """`zero mcp serve` (main.py): MCP on stdio, one JSON message a line,
    until the caller closes it. Each request is answered in a thread of its
    own (zero_wait waits), their replies written whole, one at a time."""
    paths = Paths()
    # What the launcher reports goes to stderr: stdout is the protocol's
    connection = Connection(paths, ensure=lambda: ensure_server(paths),
                            source=SOURCE)
    tools = ZeroTools(connection)
    server = McpServer(lambda headers: tools)
    writing = threading.Lock()

    def answer(line):
        reply = answer_text(server, line)
        if reply is None:
            return
        text = json.dumps(reply, ensure_ascii=False)
        with writing:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    for line in sys.stdin:
        if line.strip():
            threading.Thread(target=answer, args=(line,), daemon=True).start()
    return 0
