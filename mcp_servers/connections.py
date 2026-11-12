"""The door to the user's MCP servers (McpServers): one per ZERO server,
made by the service, handed to each session it builds (design rule 3: what
is found at run time comes through a declared door). It starts each listed
server in the background, on one thread of its own that runs the MCP SDK's
event loop, so starting, listing and calling block no session and no
request (rule 12); a session's call waits on that loop for its own answer
only, a stop at hand.

A server's life: starting, then ready (its tools offered: core's registry,
`add_found`), or failed (its error and its last words on stderr kept). A
ready server that goes away keeps its tools offered, and the next call
starts it again and goes to the new one (Pi's and OpenClaw's way); a call is
never sent twice, since its server may have done it. A server that lists new
tools (`notifications/tools/list_changed`) is listed again. At ZERO's
server's stop, each is closed as the SDK closes it: its input closed, then
SIGTERM, then SIGKILL, each after 2 s, its whole process tree.

What a session sends of the tools with its requests: each server's whole,
in the list's order, while it fits what is left of TOOLS_PERCENT of the
line; a server that doesn't fit is skipped, its tools reachable all the
same through a search (found_tools/, core's `deferred`), so a server's tools
are either all sent or all found. A server's `tools` names the ones to
offer at all.
"""

import asyncio
import collections
import concurrent.futures
import json
import os
import threading
import time
from contextlib import AsyncExitStack

from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.types import Implementation

import paths
from core import tools
from core.control import Stopped
from core.tools import ERROR, Grant, Result
from mcp_servers import adapter, listed

# The share of a session's line its servers' tools sent with each request
# may take, in characters at the token a byte ZERO's own files count (the
# skills' list takes 2%); the rest are found through a search
TOOLS_PERCENT = 5
# Whose a server's tools are, as a search's listing and answer say it
SOURCE = "the MCP server {server}"
# A server's last words on stderr kept, in characters (Pi keeps 2,000)
STDERR_CHARS = 2000
# Pages of a tool list read at most (Codex's 100)
PAGES_MOST = 100
# How often a waiting call looks at the turn's stop, in seconds
LOOK_SECONDS = 0.1
# What a waiting call allows beyond its server's own clock, in seconds
BACKSTOP_SECONDS = 5
# How long ZERO's stop gives the servers to close: the SDK's 2 s for its
# input, 2 s after SIGTERM, 2 s to reap
CLOSE_SECONDS = 7
STARTING, READY, FAILED, GONE, STOPPED = (
    "starting", "ready", "failed", "gone", "stopped")
# What an ask says a call crosses (asks.py): the server runs outside
FACT = ("The tool is the MCP server {server}'s: it runs in that server's own "
        "process, outside the sandbox.")
CROSSING_RULE = "mcp_server"
NOT_LISTED = "the MCP server {server} isn't listed now"
NOT_RUNNING = "the MCP server {server} isn't running: {why}"
GONE_NOW = ("the MCP server {server} went away during the call ({why}), so "
            "its outcome is unknown; ZERO starts it again, and a call made "
            "again goes to the new one")
TIMED_OUT = "no answer in {seconds:g} s (its call_seconds or mcp_call_seconds)"


class Server:
    """One listed MCP server, as ZERO knows it now."""

    def __init__(self, entry):
        self.entry = entry
        self.state = STARTING
        self.error = None
        self.client = None
        # The tools it offers, by the model's name: (the server's name,
        # its mcp Tool)
        self.tools = {}
        # What of its listing can't be offered, a line each
        self.notes = []
        self.protocol_version = None
        self.server_info = None
        self.stderr = collections.deque()
        self.stderr_chars = 0
        self.started_at = None
        # Set when its start ends, either way
        self.started = threading.Event()
        self.stopping = None
        self.future = None

    def heard(self, line):
        """A line it wrote on stderr, kept while it fits STDERR_CHARS."""
        self.stderr.append(line)
        self.stderr_chars += len(line)
        while self.stderr_chars > STDERR_CHARS and len(self.stderr) > 1:
            self.stderr_chars -= len(self.stderr.popleft())

    def last_words(self):
        return "".join(self.stderr).strip()

    def row(self):
        """What the user sees of it (`zero mcp list`)."""
        return {
            **self.entry.shown(), "state": self.state, "error": self.error,
            "tools": len(self.tools), "protocol_version": self.protocol_version,
        }


class McpServers:
    """The door. `listed_file()`: the user's list (listed.py); `limits`: the
    settings as they stand (mcp_start_seconds, mcp_call_seconds)."""

    def __init__(self, listed_file, limits, version=None):
        self.listed_file = listed_file
        self.limits = limits
        self.version = version or paths.changelog_version() or "0"
        self.servers = {}
        self.notes = []
        self.lock = threading.Lock()
        self.loop = None
        self.thread = None
        # The tools offered for a session's room: (line chars, change) →
        # names, worked out again after any change
        self.changes = 0
        self.fits = {}

    # ------------------------------------------------------ start and stop

    def start(self):
        """The loop's thread, then every listed server, in the background."""
        if self.loop is None:
            self.loop = asyncio.new_event_loop()
            self.thread = threading.Thread(
                target=self.loop.run_forever, name="mcp-servers", daemon=True)
            self.thread.start()
        found = listed.read(self.listed_file())
        self.notes = found.notes
        for entry in found.entries:
            self.start_server(entry)

    def start_server(self, entry):
        """Start one server in the background; one by that name stops first."""
        self.stop_server(entry.name)
        server = Server(entry)
        with self.lock:
            self.servers[entry.name] = server
        self.launch(server)
        return server

    def launch(self, server):
        server.state, server.error = STARTING, None
        server.started.clear()
        server.started_at = time.time()
        server.future = asyncio.run_coroutine_threadsafe(
            self.serve(server), self.loop)

    def stop_server(self, name, seconds=CLOSE_SECONDS):
        """Stop a server and take its tools back; False where none runs."""
        with self.lock:
            server = self.servers.pop(name, None)
        if server is None:
            return False
        self.withdraw(server)
        self.close(server, seconds)
        server.state = STOPPED
        return True

    def close(self, server, seconds):
        if server.stopping is not None and self.loop is not None:
            self.loop.call_soon_threadsafe(server.stopping.set)
        if server.future is not None:
            try:
                server.future.result(timeout=seconds)
            except Exception:
                server.future.cancel()

    def stop(self):
        """Every server closed, side by side, then the loop: ZERO's stop."""
        with self.lock:
            running = list(self.servers.values())
            self.servers = {}
        if self.loop is None:
            return
        for server in running:
            self.withdraw(server)
            if server.stopping is not None:
                self.loop.call_soon_threadsafe(server.stopping.set)
        deadline = time.monotonic() + CLOSE_SECONDS
        for server in running:
            if server.future is None:
                continue
            try:
                server.future.result(max(0.0, deadline - time.monotonic()))
            except Exception:
                server.future.cancel()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=CLOSE_SECONDS)
        self.loop = None

    # ------------------------------------------------- a server's whole life

    async def serve(self, server):
        """Start the server, list its tools, then hold it open until told to
        stop; what goes wrong is kept as its error."""
        server.stopping = asyncio.Event()
        entry = server.entry
        start_seconds = entry.start_seconds or self.limits["mcp_start_seconds"]
        reader = None
        try:
            async with AsyncExitStack() as stack:
                if entry.transport == "stdio":
                    reader = StderrReader(server)
                    transport = stdio_client(StdioServerParameters(
                        command=entry.command, args=list(entry.args),
                        env=dict(entry.env) or None, cwd=entry.cwd,
                        encoding_error_handler="replace",
                    ), errlog=reader.stream)
                else:
                    http = await stack.enter_async_context(
                        create_mcp_http_client(headers=dict(entry.headers)))
                    transport = streamable_http_client(entry.url,
                                                       http_client=http)

                async def heard(message):
                    # A notification, bare or in its wrapper
                    method = getattr(message, "method", None) or getattr(
                        getattr(message, "root", None), "method", None)
                    if method == "notifications/tools/list_changed":
                        asyncio.ensure_future(self.list_again(server))

                client = Client(
                    transport, read_timeout_seconds=start_seconds,
                    client_info=Implementation(name="zero",
                                               version=self.version),
                    message_handler=heard, cache=None,
                )
                await asyncio.wait_for(stack.enter_async_context(client),
                                       start_seconds)
                if reader is not None:
                    reader.spawned()
                server.client = client
                server.protocol_version = client.protocol_version
                info = client.server_info
                server.server_info = (
                    None if info is None
                    else {"name": info.name, "version": info.version})
                listing = await asyncio.wait_for(self.listing(client),
                                                 start_seconds)
                self.offer(server, listing)
                server.state = READY
                server.started.set()
                await server.stopping.wait()
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError) or (
                    server.stopping is not None and server.stopping.is_set()):
                server.state = STOPPED
            else:
                server.error = self.failed_text(server, error)
                server.state = GONE if server.tools else FAILED
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            server.client = None
            if reader is not None:
                reader.done()
            server.started.set()

    def failed_text(self, server, error):
        if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
            seconds = (server.entry.start_seconds
                       or self.limits["mcp_start_seconds"])
            text = (f"it didn't start in {seconds:g} s (its start_seconds or "
                    "mcp_start_seconds)")
        else:
            text = adapter.reason(error)
        words = server.last_words()
        return f"{text}; its last words on stderr: {words}" if words else text

    async def listing(self, client):
        """Every tool the server lists, page by page."""
        found, cursor = [], None
        for _ in range(PAGES_MOST):
            page = await client.list_tools(cursor=cursor)
            found += page.tools
            cursor = page.next_cursor
            if cursor is None:
                break
        return found

    async def list_again(self, server):
        """The server said its tools changed: listed again and offered."""
        client = server.client
        if client is None:
            return
        try:
            listing = await self.listing(client)
        except Exception as error:
            server.notes = [f"listing its tools again failed: "
                            f"{adapter.reason(error)}"]
            return
        self.withdraw(server)
        self.offer(server, listing)

    # -------------------------------------------------------------- tools

    def offer(self, server, listing):
        """The server's tools into core's registry, as found tools: those
        its entry names (every one without `tools`) whose arguments are an
        object's."""
        wanted = server.entry.tools
        offered, notes, entries = {}, [], []
        for tool in listing:
            if wanted is not None and tool.name not in wanted:
                continue
            fitted = adapter.parameters(tool.input_schema)
            if fitted is None:
                notes.append(f"{tool.name}: its arguments aren't an object's, "
                             "which the model's API takes; left out")
                continue
            name = adapter.tool_name(server.entry.name, tool.name)
            schema, extra = fitted
            offered[name] = (tool.name, tool)
            entries.append(self.found_tool(server, name, tool, schema, extra))
        if wanted is not None:
            missing = sorted(set(wanted) - {t.name for t in listing})
            if missing:
                notes.append("its entry names tools it doesn't list: "
                             + ", ".join(missing))
        server.tools, server.notes = offered, notes
        tools.add_found(entries)
        self.changed()

    def withdraw(self, server):
        tools.remove_found(server.tools)
        self.changed()

    def changed(self):
        with self.lock:
            self.changes += 1
            self.fits = {}

    def found_tool(self, server, name, tool, schema, extra):
        """The tool as core's registry offers it (tools.found_tool)."""
        server_name, tool_name = server.entry.name, tool.name

        def run(args, context):
            door = context.get("mcp_servers")
            if door is None:
                return Result(ERROR + NOT_LISTED.format(server=server_name),
                              failed=True)
            return door.call(server_name, tool_name, args, context.get("stop"))

        def crossing(args):
            # No reason argument: the fact is the ask's whole reason
            return {"rule": CROSSING_RULE,
                    "fact": FACT.format(server=server_name)}

        return tools.found_tool(
            name, adapter.description(server_name, tool.description), schema,
            run, ready=lambda context: self.offers(name, context),
            reachable=lambda context: self.reaches(name, context),
            source=SOURCE.format(server=server_name), name_at_source=tool_name,
            crossing=crossing, extra_arguments=extra,
            grant=Grant("tools", lambda args: [name],
                        lambda grant: "the MCP tools "
                        + ", ".join(grant["tools"])),
            output_schema=tool.output_schema,
        )

    def offers(self, name, context):
        """Whether a session sends the tool with its requests now: it can
        reach it (`reaches`), and its server fits the session's room."""
        if not self.reaches(name, context):
            return False
        line_chars = context.get("line_chars")
        room = line_chars() * TOOLS_PERCENT // 100 if line_chars else None
        return name in self.fitting(room)

    def reaches(self, name, context):
        """Whether a session can call the tool now: its door is this one, and
        its server is ready (or went away, to start again at the call)."""
        if context.get("mcp_servers") is not self:
            return False
        return name in self.offered_names()

    def offered_names(self):
        """Every tool of the servers that are ready, or gone to start again:
        a server gone, or starting again after it went, keeps its tools, so
        the next call goes to the new one."""
        with self.lock:
            key = ("all", self.changes)
            if key not in self.fits:
                self.fits[key] = frozenset(
                    name for server in self.servers.values()
                    if server.tools and server.state not in (FAILED, STOPPED)
                    for name in server.tools)
            return self.fits[key]

    def fitting(self, room):
        """The names sent within `room` characters (None: no bound): each
        server's tools whole, in the list's order, while their schemas fit
        what is left; a server that doesn't fit is skipped."""
        with self.lock:
            key = (room, self.changes)
            if key in self.fits:
                return self.fits[key]
            names, used = set(), 0
            for server in self.servers.values():
                if not server.tools or server.state in (FAILED, STOPPED):
                    continue
                own = [name for name in server.tools if name in tools.FOUND]
                size = sum(
                    len(json.dumps(tools.FOUND[name]["schema"],
                                   ensure_ascii=False))
                    for name in own)
                if room is not None and used + size > room:
                    continue
                names.update(own)
                used += size
            self.fits[key] = frozenset(names)
            return self.fits[key]

    # --------------------------------------------------------------- calls

    def call(self, server_name, tool_name, args, stop=None):
        """Call a server's tool from a session's thread: its Result. A stop
        returns at once (Stopped), and the SDK tells the server the call is
        cancelled. A server gone since its last call starts again first."""
        with self.lock:
            server = self.servers.get(server_name)
        if server is None:
            return adapter.failure(server_name, tool_name,
                                   NOT_LISTED.format(server=server_name))
        if server.state == GONE:
            self.relaunch(server)
        start_seconds = (server.entry.start_seconds
                         or self.limits["mcp_start_seconds"])
        if not self.wait(server.started, start_seconds, stop):
            return adapter.failure(server_name, tool_name, NOT_RUNNING.format(
                server=server_name, why="it is still starting"))
        if server.state != READY or server.client is None:
            return adapter.failure(server_name, tool_name, NOT_RUNNING.format(
                server=server_name, why=server.error or server.state))
        seconds = server.entry.call_seconds or self.limits["mcp_call_seconds"]
        client = server.client
        future = asyncio.run_coroutine_threadsafe(
            client.call_tool(tool_name, args, read_timeout_seconds=seconds),
            self.loop)
        deadline = time.monotonic() + seconds + BACKSTOP_SECONDS
        while not future.done():
            if stop is not None and stop.is_set():
                future.cancel()
                raise Stopped("stop")
            if time.monotonic() > deadline:
                future.cancel()
                return adapter.failure(server_name, tool_name,
                                       TIMED_OUT.format(seconds=seconds))
            concurrent.futures.wait([future], timeout=LOOK_SECONDS)
        try:
            result = future.result()
        except Exception as error:
            return self.call_failed(server, tool_name, error, seconds)
        return adapter.answer(server_name, tool_name, result)

    def call_failed(self, server, tool_name, error, seconds):
        """A call that raised: the server's error, a timeout, or the server
        gone (started again for the next call)."""
        text = adapter.reason(error)
        if "timed out" in text.lower():
            return adapter.failure(server.entry.name, tool_name,
                                   TIMED_OUT.format(seconds=seconds))
        if server.client is not None and not connection_broken(error):
            return adapter.failure(server.entry.name, tool_name, text)
        server.error = text
        server.state = GONE
        self.relaunch(server)
        return adapter.failure(server.entry.name, tool_name, GONE_NOW.format(
            server=server.entry.name, why=text))

    def relaunch(self, server):
        """A server gone, started again, its tools still offered."""
        with self.lock:
            if self.servers.get(server.entry.name) is not server:
                return
            if server.state == STARTING:
                return
            server.state = STARTING
        if server.stopping is not None:
            self.loop.call_soon_threadsafe(server.stopping.set)
        if server.future is not None:
            try:
                server.future.result(timeout=CLOSE_SECONDS)
            except Exception:
                server.future.cancel()
        server.stderr.clear()
        server.stderr_chars = 0
        self.launch(server)

    @staticmethod
    def wait(event, seconds, stop=None):
        """Wait for `event` at most `seconds`, a stop raising Stopped."""
        deadline = time.monotonic() + seconds
        while not event.wait(LOOK_SECONDS):
            if stop is not None and stop.is_set():
                raise Stopped("stop")
            if time.monotonic() > deadline:
                return False
        return True

    # -------------------------------------------------------- looking

    def starting(self):
        """The servers whose start hasn't ended."""
        with self.lock:
            return [s for s in self.servers.values() if not s.started.is_set()]

    def wait_started(self, seconds, stop=None):
        """Wait while any server is starting, at most `seconds`."""
        deadline = time.monotonic() + seconds
        for server in self.starting():
            left = deadline - time.monotonic()
            if left <= 0 or not self.wait(server.started, left, stop):
                return

    def rows(self):
        """Each listed server's state, in the list's order."""
        with self.lock:
            return [server.row() for server in self.servers.values()]

    def details(self, name):
        """One server: its row, its tools as the model calls them, what of
        its listing can't be offered, its last words on stderr; None where
        it isn't listed."""
        with self.lock:
            server = self.servers.get(name)
        if server is None:
            return None
        return {
            **server.row(), "server_info": server.server_info,
            "tool_list": [
                {"name": model_name, "tool": tool_name,
                 "description": (tool.description or "").strip()}
                for model_name, (tool_name, tool) in server.tools.items()],
            "notes": server.notes, "stderr": server.last_words(),
        }

    def wait_for(self, name, seconds):
        """Wait for one server's start to end, at most `seconds`."""
        with self.lock:
            server = self.servers.get(name)
        if server is not None:
            server.started.wait(seconds)


def connection_broken(error):
    """Whether a call's error says the server is gone (its pipe or stream
    closed), not that it answered with an error."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    names = {cls.__name__ for cls in type(error).__mro__}
    broken = {"ClosedResourceError", "BrokenResourceError", "EndOfStream",
              "BrokenPipeError", "ConnectionError", "RemoteProtocolError"}
    return bool(names & broken) or "connection closed" in str(error).lower()


class StderrReader:
    """A command's stderr, read on a thread of its own into its server's
    last words. `stream`: the pipe's end the command writes to."""

    def __init__(self, server):
        self.server = server
        read_end, write_end = os.pipe()
        self.stream = os.fdopen(write_end, "w")
        self.reading = os.fdopen(read_end, "r", errors="replace")
        self.thread = threading.Thread(
            target=self.read, name=f"mcp-stderr-{server.entry.name}",
            daemon=True)
        self.thread.start()

    def read(self):
        try:
            for line in self.reading:
                self.server.heard(line)
        except (OSError, ValueError):
            pass
        finally:
            self.reading.close()

    def spawned(self):
        """The command holds its own copy of the pipe now; ZERO's is closed,
        so the reader ends when the command does."""
        self.done()

    def done(self):
        try:
            self.stream.close()
        except OSError:
            pass
