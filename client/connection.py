"""JSON-line requests and a closeable, long-lived watch stream; each request
built from the protocol's messages (protocol/messages.py).

A connection that fails or drops before its reply is NotRunning: the caller may
start a server and send the same request again, with the same message id, which
the server admits once. A server that takes the connection but never answers is
NoAnswer: the request may be running there, and most carry no id to admit once,
so it isn't sent again.
"""

import json
import os
import socket
import uuid

from protocol.messages import request as build


class NotRunning(Exception):
    pass


class NoAnswer(NotRunning):
    """Connected, but no reply in time: a server that hangs, or one still
    starting."""


class ProtocolError(Exception):
    pass


def connect(path, timeout=30):
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(str(path))
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError) as error:
        conn.close()
        raise NotRunning(
            f"ZERO's server isn't running (no server at {path}): {error}"
        ) from None
    except BaseException:
        conn.close()
        raise
    return conn


# Seconds a connect may take: the server's test call waits up to a minute
CONNECT_WAIT_SECONDS = 90
# Seconds a model's features may take to measure: four small calls, each
# with a short thought on a thinking model
FEATURES_WAIT_SECONDS = 300


def request(path, payload, timeout=30):
    try:
        with connect(path, timeout) as conn:
            conn.sendall((json.dumps(payload) + "\n").encode())
            with conn.makefile(encoding="utf-8") as stream:
                line = stream.readline()
    except TimeoutError:
        raise NoAnswer(f"ZERO's server did not answer at {path}") from None
    except (ConnectionResetError, BrokenPipeError):
        # A server that stopped listening as this connected (its idle exit)
        raise NotRunning("ZERO's server closed the connection") from None
    if not line:
        raise NotRunning("ZERO's server closed the connection without a reply")
    try:
        return json.loads(line)
    except ValueError:
        raise ProtocolError("ZERO's server sent an invalid JSON reply") from None


class Watch:
    """A stream of events: a session's (`session/watch`), or the session
    list's (`session/list` with `watch`, `payload` given)."""

    def __init__(self, path, session=None, after=0, payload=None):
        self.conn = connect(path)
        self.stream = None
        # The reason the server gave when it closed this watch, if it did
        self.bye = None
        payload = payload or build("session/watch", session=session, after=after)
        try:
            self.conn.sendall((json.dumps(payload) + "\n").encode())
            self.conn.settimeout(None)
            self.stream = self.conn.makefile(encoding="utf-8")
        except (ConnectionResetError, BrokenPipeError):
            self.conn.close()
            raise NotRunning("ZERO's server closed the connection") from None
        except BaseException:
            self.conn.close()
            raise

    def __iter__(self):
        """Events until the server closes the watch; a `bye` ends it, and its
        reason is kept in `bye`."""
        for line in self.stream:
            event = json.loads(line)
            if event.get("ok") is False:
                raise ProtocolError(event["error"])
            if "bye" in event:
                self.bye = event["bye"]
                return
            yield event

    def close(self):
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        if self.stream is not None:
            self.stream.close()
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Connection:
    """`ensure()` finds or starts a server, and checks its versions: called before
    the first request, and again when a request finds no server."""

    def __init__(self, paths, ensure=None, source=None):
        self.paths = paths
        self.ensure = ensure
        self.source = source or f"terminal-{os.getpid()}"
        self.checked = False

    def ready(self):
        if self.ensure is not None and not self.checked:
            self.ensure()
            self.checked = True

    def call(self, payload, timeout=30):
        self.ready()
        try:
            reply = request(self.paths.socket, payload, timeout)
        except NoAnswer:
            raise
        except NotRunning:
            if self.ensure is None:
                raise
            self.ensure()
            reply = request(self.paths.socket, payload, timeout)
        if not reply["ok"]:
            raise ProtocolError(reply["error"])
        return reply

    def send(self, text, session=None, mode=None, model=None, reply_to=None,
             workspace=None, when_busy="steer", choice=None, thinking_effort=None,
             rounds=False):
        """`reply_to`: the open ask's id, for a line typed at its prompt.
        `workspace` and `thinking_effort`: a new session's folder and thinking.
        `when_busy`: "queue" makes a follow-up that waits for the running
        turn's answer. `choice`: one of the open ask's choices, picked at its
        box. `rounds`: to be worked on in rounds."""
        msg_id = str(uuid.uuid4())
        request = build(
            "session/send", id=msg_id, text=text, session=session, mode=mode,
            model=model, source=self.source, reply_to=reply_to,
            workspace=workspace, when_busy=when_busy,
        )
        if thinking_effort is not None:
            request["thinking_effort"] = thinking_effort
        if choice is not None:
            request["choice"] = choice
        if rounds:
            request["rounds"] = True
        return {**self.call(request), "id": msg_id}

    def continue_session(self, session):
        """`session/continue`: a turn that ended without an answer goes on."""
        msg_id = str(uuid.uuid4())
        return {**self.call(build(
            "session/continue", id=msg_id, session=session, source=self.source,
        )), "id": msg_id}

    def set(self, session, mode=None, model=None, workspace=None, thinking_effort=None,
            judge_model=None, judge_thinking_effort=None):
        """Change a session's mode, model, workspace, thinking effort or judge from its
        next turn; a `judge_model` of "" is the session's own model again."""
        given = {"mode": mode, "model": model, "workspace": workspace,
                 "thinking_effort": thinking_effort, "judge_model": judge_model,
                 "judge_thinking_effort": judge_thinking_effort}
        return self.call(build("session/set", session=session, **{
            name: value for name, value in given.items() if value is not None
        }))

    def grants(self, session):
        """The grants that hold in a session."""
        return self.call(build("grant/list", session=session))["grants"]

    def forget_grant(self, session, grant):
        """Take a grant back."""
        return self.call(build("grant/forget", session=session, grant=grant))

    def hello(self):
        return self.call(build("server/hello"))

    def models(self):
        """Every server the server can reach, with its models by the names a
        session takes, or why it lists none: the server's reply as it is."""
        return self.call(build("model/list"))

    def features(self, model, test=False):
        """What a model honours of its API's features, as the server measured
        it (measured now when never, or `test`): the server's reply. Measuring
        calls the model a few times, so the wait is long."""
        fields = {"test": True} if test else {}
        return self.call(build("model/features", model=model, **fields),
                         timeout=FEATURES_WAIT_SECONDS)

    def servers(self):
        """Every model server the server knows, whether its key is there, the
        default model and why a session on it couldn't start (None: it
        can): the server's reply as it is."""
        return self.call(build("model/server/list"))

    def probe(self, name=None, url=None, key=None):
        """A server's models, for a pick list: (the url that answered,
        [{"id", "context_window"}])."""
        fields = {k: v for k, v in (("name", name), ("url", url), ("key", key))
                  if v is not None}
        reply = self.call(build("model/server/probe", **fields))
        return reply["url"], reply["models"]

    def connect(self, name, model, url=None, key=None, context_window=None,
                title=None, default=None):
        """A server's model tested, then saved (and the default if none is
        set, or if asked): the server's reply. A local server may first load
        the model, so the wait is long."""
        fields = {"url": url, "key": key, "context_window": context_window,
                  "title": title,
                  "default": default}
        given = {k: v for k, v in fields.items() if v is not None}
        request = build("model/server/connect", name=name, model=model, **given)
        return self.call(request, timeout=CONNECT_WAIT_SECONDS)

    def disconnect(self, name):
        """The server's key and the user's lines for it taken out."""
        return self.call(build("model/server/disconnect", name=name))

    def mcp_servers(self):
        """(the MCP servers listed, each with its state; what the list says
        that ZERO can't use)."""
        reply = self.call(build("mcp/list"))
        return reply["servers"], reply["notes"]

    def mcp_server(self, name):
        """One MCP server, with its tools and its last words on stderr."""
        return self.call(build("mcp/get", name=name))["server"]

    def mcp_add(self, name, **fields):
        """An MCP server listed, then started: the server's reply. Its start
        may take the server's start time, so the wait is long."""
        given = {k: v for k, v in fields.items() if v is not None}
        request = build("mcp/add", name=name, **given)
        return self.call(request, timeout=CONNECT_WAIT_SECONDS)

    def mcp_remove(self, name):
        """An MCP server taken off the list and stopped."""
        return self.call(build("mcp/remove", name=name))

    def settings(self):
        """ZERO's settings, each with its value, and what is wrong with
        config.toml (None if nothing): the server's to read and write."""
        reply = self.call(build("setting/list"))
        return reply["settings"], reply["error"]

    def set_setting(self, name, value):
        """Write one of ZERO's settings, through the server."""
        return self.call(build("setting/set", name=name, value=value))

    def mark_seen(self, session, seq):
        return self.call(build("session/mark_seen", session=session, seq=seq))

    def rename(self, session, title):
        return self.call(build("session/rename", session=session, title=title))

    def compact(self, session, instructions=None):
        """`session/compact`'s reply: whether it compacts "now" or
        "before_next_step". `instructions`: the user's words on what the
        handoff keeps."""
        words = {"instructions": instructions} if instructions else {}
        return self.call(build("session/compact", session=session, **words))

    def user_messages(self, session):
        """The user's own messages on the session's current branch that start
        a turn, newest first (seq, time, text): where a rewind or fork begins."""
        return self.call(build("session/user_messages", session=session))["messages"]

    def rewind(self, session, seq):
        """`session/rewind`'s reply: the rewind's record, and the message's
        text for the input."""
        return self.call(build("session/rewind", session=session, seq=seq))

    def fork(self, session, seq=None):
        """`session/fork`'s reply: the new session's row, and the message's
        text for its input ("" when all of the conversation was taken)."""
        fields = {} if seq is None else {"seq": seq}
        return self.call(build("session/fork", session=session,
                               source=self.source, **fields))

    def archive(self, session, archived=True):
        name = "session/archive" if archived else "session/unarchive"
        return self.call(build(name, session=session))

    def delete(self, session):
        """Where the session's files went: the desktop's trash or ZERO's."""
        return self.call(build("session/delete", session=session))["trash"]

    def stop(self, session):
        return self.call(build(
            "session/stop", id=str(uuid.uuid4()), session=session, source=self.source,
        ))

    def sessions(self, archived=None, workspace=None):
        """Every session; or only the archived ones (True) or the others
        (False); or only those whose workspace is exactly that folder."""
        return self.call(build(
            "session/list", archived=archived, workspace=workspace,
        ))["sessions"]

    def read(self, session, after=0, limit=None, before=None):
        """`session/read`'s whole reply: the records after the seq (and
        before `before`), or the newest `limit` of them, how many came before
        them, and what the session's next turn runs with (`runs_with`)."""
        # A limit of null asks for them all; left out, the server's window (50)
        return self.call(build(
            "session/read", session=session, after=after, limit=limit, before=before,
        ))

    def history(self, session, after=0, limit=None, before=None):
        """(records, earlier_count), as `read` gives them."""
        reply = self.read(session, after, limit, before)
        return reply["records"], reply["earlier_count"]

    def runs_with(self, session):
        """What the session's next turn runs with: its mode, model, workspace,
        effort, judge_model (None: the session's own) and judge_thinking_effort."""
        return self.read(session, limit=0)["runs_with"]

    def jobs(self, session=None):
        """A session's jobs, ended ones too; with none, every running job."""
        fields = {} if session is None else {"session": session}
        return self.call(build("job/list", **fields))["jobs"]

    def search(self, query=None, session=None, around=None, count=None):
        """What every session's log holds (session/search): the reply."""
        given = {"query": query, "session": session, "around": around,
                 "count": count}
        fields = {name: value for name, value in given.items()
                  if value is not None}
        return self.call(build("session/search", **fields))

    def watch(self, session, after=0):
        return self.stream(build("session/watch", session=session, after=after))

    def watch_list(self, archived=False, workspace=None):
        """The session list, then the whole list again at each change."""
        return self.stream(build("session/list", watch=True, archived=archived,
                                 workspace=workspace))

    def stop_job(self, job):
        """Whether the job was running and is stopped now."""
        return self.call(build("job/stop", job=job))

    def rounds(self, session=None):
        """A session's work in rounds, ended runs too; with none, every open
        one."""
        fields = {} if session is None else {"session": session}
        return self.call(build("rounds/list", **fields))["rounds"]

    def stop_rounds(self, rounds):
        """Whether the work in rounds was open and is stopped now."""
        return self.call(build("rounds/stop", rounds=rounds))

    def stream(self, payload):
        self.ready()
        try:
            return Watch(self.paths.socket, payload=payload)
        except NotRunning:
            if self.ensure is None:
                raise
            self.ensure()
            return Watch(self.paths.socket, payload=payload)
