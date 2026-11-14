"""Who is on the other end of the socket: the user's own client, or one
of ZERO's own commands.

Any program running as the user can open the socket, and in auto+ (or a call
let out of the sandbox) that includes the commands ZERO runs: `zero -p yes
--session ID` answers another session's ask as the user's terminal would. ZERO's
own commands may look, and only the user's own client sends, answers, stops
or changes anything.

A peer is ZERO's own when its process descends from the server's, or, where
the server runs in its unit (systemd's transient `zero-server`, named to it in
ZERO_SERVER_UNIT by client/launcher.py, and the server's cgroup is that unit's),
when it lives in the server's cgroup: a command whose parent is gone is taken
in by systemd but stays in the unit. What this leaves:
a command that starts `zero` in a unit of its own (`systemd-run --user
--scope zero …`), which the judge sees as it is. Sandboxed commands can't
reach the socket at all (sandbox/sandbox.py hides /run/user).

A workflow, one of ZERO's own commands that calls ZERO, may also send the
messages a service names in `workflow_messages`, for the session whose
command it is: each command leads a Unix session of its own (jobs/ starts it
so), which every process it starts keeps, so a peer's Unix session names the
command, and the service that started it names the session.

Where the socket can't tell who connects (`SO_PEERCRED` gives nothing: a
system without it, a connection that isn't a Unix socket's), the request
proves the user with the app token, the text of a file only the user reads
(paths.app_token), made on first use and kept across restarts (Jupyter's
and Tailscale's way). A wrong token is refused wherever it comes; a right
one never lifts a ZERO command's limits, which the process tree decides.

A relay (ZERO's HTTP door) sends on its own clients' behalf and names each
request's client process in `relayed_for_pid` where the kernel told it (a
loopback connection's owner): the server then decides by that process as if
it had connected itself (`relayed`). Heeded only from the user's own client,
so a ZERO command can't pass for another.
"""

import hmac
import os
import secrets
import socket
import struct

import paths as home_paths
from protocol.messages import SESSION_ID_ENV

# Where client/launcher.py names the server's unit to it
UNIT_ENV = "ZERO_SERVER_UNIT"
UNSET = object()


def refused(looking_messages=(), workflow_messages=()):
    """The no one of ZERO's own commands gets; `looking_messages`: the messages that
    only look, the kernel's and the services'; `workflow_messages`: those a
    command may send for its own session."""
    named = ", ".join(looking_messages)
    text = (
        f"ZERO's own commands can look at sessions over this socket ({named}) "
        "but not send, answer, stop or change anything: that comes from the "
        "user's own client."
    )
    if workflow_messages:
        text += (
            f" A command may send {', '.join(workflow_messages)} for the "
            "session whose command it is, named in for_session (zero does so "
            f"where ${SESSION_ID_ENV} is set)."
        )
    return text


def not_its_session(named, owner):
    """The no a command gets that names a session it isn't a command of."""
    if owner is None:
        return (
            "this process isn't one of a session's commands that ZERO can "
            "tell (a command keeps its session's mark while it runs in the "
            "Unix session ZERO started it in), so it sends nothing for "
            f"session {named!r}."
        )
    return (f"this process is a command of session {owner}, so it sends for "
            f"that session, not {named!r}.")


def unix_session(pid):
    """The Unix session a process is in (/proc's field 6), or None."""
    try:
        text = open(f"/proc/{pid}/stat").read()
    except (OSError, TypeError):
        return None
    fields = text.rpartition(")")[2].split()
    return int(fields[3]) if len(fields) > 3 else None


def peer_of(conn):
    """(pid, uid) of the process on the other end of a Unix socket, or
    (None, None) where the socket can't tell."""
    try:
        data = conn.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
    except (OSError, AttributeError):
        return None, None
    pid, uid, _ = struct.unpack("3i", data)
    return (pid or None), (uid if pid else None)


def peer_pid(conn):
    """The process on the other end of a Unix socket, or None."""
    return peer_of(conn)[0]


# The no for a request whose sender isn't proven
UNPROVEN = ("the socket can't tell who connects here, so a request proves the "
            "user with `token`, the text of the token file ({path})")
WRONG_TOKEN = "wrong token: it isn't the text of the token file ({path})"
OTHER_USER = "this server is another user's"
TOKEN_BYTES = 32


class AppToken:
    """The token file: made on first use (whole, the user's alone), read
    again when it changes, so removing it and starting the server again
    makes a new one."""

    def __init__(self, path):
        self.path = path
        self.seen = None
        self.text = None

    def make(self):
        """The file, made now if missing."""
        if not self.path.exists():
            home_paths.write_whole(self.path, secrets.token_urlsafe(TOKEN_BYTES)
                                   + "\n", mode=0o600)
        return self.path

    def current(self):
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self.make()
            stat = self.path.stat()
        if (stat.st_mtime_ns, stat.st_ino) != self.seen:
            self.text = self.path.read_text().strip()
            self.seen = (stat.st_mtime_ns, stat.st_ino)
        return self.text

    def matches(self, given):
        """Whether `given` is the token, compared in constant time."""
        if not isinstance(given, str) or not given:
            return False
        return hmac.compare_digest(given.encode(), self.current().encode())


def proven(conn, token, app_token):
    """The peer's pid where the socket proves it (a Unix socket's process of
    this user), or None where the token does; PermissionError, with why,
    for a peer neither proves or a wrong token."""
    pid, uid = peer_of(conn)
    if token is not None and not app_token.matches(token):
        raise PermissionError(WRONG_TOKEN.format(path=app_token.path))
    if pid is not None:
        if uid != os.getuid():
            raise PermissionError(OTHER_USER)
        return pid
    if token is None:
        raise PermissionError(UNPROVEN.format(path=app_token.path))
    return None


def owner(pid):
    """The user a process runs as (its real uid), or None once it has gone."""
    try:
        text = open(f"/proc/{pid}/status").read()
    except (OSError, TypeError):
        return None
    for line in text.splitlines():
        if line.startswith("Uid:"):
            return int(line.split()[1])
    return None


# The no for a relay naming another user's process
RELAYED_OTHER_USER = "the process this was relayed for is another user's"


def relayed(pid, relayed_for, peers):
    """The process a request is held to: `relayed_for`, the one a relay
    (ZERO's HTTP door) names as its client, where the relay itself is the
    user's own client; else the connection's own `pid`. A ZERO command that
    names another process is held to itself, so naming one never lifts a
    limit. PermissionError for another user's process."""
    if relayed_for is None:
        return pid
    if type(relayed_for) is not int or relayed_for < 2:
        raise PermissionError("relayed_for_pid is a process's id")
    if pid is not None and peers.is_zero_command(pid):
        return pid
    found = owner(relayed_for)
    if found is not None and found != os.getuid():
        raise PermissionError(RELAYED_OTHER_USER)
    return relayed_for


def parent(pid):
    """A process's parent's pid, or None once it has gone."""
    try:
        text = open(f"/proc/{pid}/stat").read()
    except OSError:
        return None
    # The name in brackets may hold spaces; the state and the parent follow it
    fields = text.rpartition(")")[2].split()
    return int(fields[1]) if len(fields) > 1 else None


def cgroup(pid):
    try:
        return open(f"/proc/{pid}/cgroup").read()
    except OSError:
        return None


class Peers:
    def __init__(self, pid=None, unit=UNSET):
        """`unit`: the server's unit's name (client/launcher.py sets it), or None
        for none."""
        self.pid = pid or os.getpid()
        if unit is UNSET:
            unit = os.environ.get(UNIT_ENV)
        # Only the unit's own cgroup counts: a terminal's or a test's is
        # shared with the user's clients
        mine = cgroup(self.pid)
        in_unit = unit and mine and mine.rstrip().endswith(f"/{unit}.service")
        self.cgroup = mine if in_unit else None

    def is_zero_command(self, pid):
        """Whether process `pid` is one of ZERO's own commands. The server's
        own process is not: only a test connects from it."""
        if pid is None or pid <= 1 or pid == self.pid:
            return False
        if self.cgroup is not None and cgroup(pid) == self.cgroup:
            return True
        seen = set()
        while pid is not None and pid > 1 and pid not in seen:
            seen.add(pid)
            pid = parent(pid)
            if pid == self.pid:
                return True
        return False
