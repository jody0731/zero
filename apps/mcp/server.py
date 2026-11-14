"""`zero mcp serve`: ZERO as an MCP server on stdio, so another agent (Claude Code,
Codex, any MCP client) hands it work: `claude mcp add zero -- zero mcp serve`.

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
The server is the caller's client, a process of the caller's and not of
ZERO's; run as one of a ZERO session's commands (a workflow, $ZERO_SESSION_ID
set), zero_start hands the task to a subagent of that session, as `zero -p`
does there.
"""

import threading
import time
from typing import Any
from pathlib import Path

from client.connection import (
    Connection, NotRunning, ProtocolError, command_session,
)
from client.launcher import ensure_server
from client.result import Result, latest_turn
from paths import Paths

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
    """The four tools as plain methods over a Connection, for any face."""

    def __init__(self, connection):
        self.connection = connection

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
        working_for = command_session()
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


def reply_or_error(work):
    """`work()`'s reply, or its error as the tool's (mcp's ToolError)."""
    from mcp.server.mcpserver.exceptions import ToolError

    try:
        return work()
    except ERRORS as error:
        raise ToolError(str(error)) from None


def server(worker):
    """The MCP server: the Worker's methods as tools (mcp's MCPServer)."""
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("zero", title="Z.E.R.O.", instructions=INSTRUCTIONS)

    @mcp.tool(description=(
        "Hand ZERO a task: a new session in `workspace` (a folder's absolute "
        "path), started at once; returns its id, its mode and the mode's "
        "line (its sandbox, and who answers ZERO's questions: you with "
        "zero_reply, or ZERO's judge). `mode`: one of ZERO's modes by name "
        "(a name it doesn't know is refused with the list); left out, the "
        "user's default. `model`: left out, the user's default."
    ))
    def zero_start(task: str, workspace: str, mode: str | None = None,
                   model: str | None = None) -> dict[str, Any]:
        return reply_or_error(lambda: worker.start(task, workspace, mode, model))

    @mcp.tool(description=(
        "Wait for a ZERO session to settle, up to `seconds` (300; 3600 at "
        "most). Returns session, outcome (answered, waiting: a question "
        "waits in `ask`, stopped, failed, or running when the seconds ran "
        "out), answer, ask, tokens and seconds."
    ))
    def zero_wait(session: str, seconds: int = WAIT_SECONDS) -> dict[str, Any]:
        return reply_or_error(lambda: worker.wait(session, seconds))

    @mcp.tool(description=(
        "Answer the question waiting in a ZERO session, in words (`text`) or "
        "with one of the choices its ask lists (`choices`, each with its "
        "words in `choice_labels`). With no question waiting, `text` is a "
        "follow-up message. Then zero_wait."
    ))
    def zero_reply(session: str, text: str | None = None,
                   choice: str | None = None) -> dict[str, Any]:
        return reply_or_error(lambda: worker.reply(session, text, choice))

    @mcp.tool(description="Stop a ZERO session's running turn.")
    def zero_stop(session: str) -> dict[str, Any]:
        return reply_or_error(lambda: worker.stop(session))

    return mcp


def serve():
    """`zero mcp serve` (main.py): serve on stdio until the caller closes it."""
    paths = Paths()
    # What the launcher reports goes to stderr: stdout is the protocol's
    connection = Connection(paths, ensure=lambda: ensure_server(paths),
                            source=SOURCE)
    server(ZeroTools(connection)).run("stdio")
    return 0
