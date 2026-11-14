"""Find ZERO's server, or start one: the server on demand.

`server/hello` with a short timeout says whether a server runs. When none does, the
client takes start.lock, says hello again (another terminal may have just
started one) and only then starts it: as a transient systemd user unit named
after its home (`zero-server` for ~/.zero), where the user's systemd answers,
else as a detached process. It
polls until the server answers. A server that takes the connection but never
answers is reported with its pid, never replaced by a second one. A server with
another build (the code changed since it started) is restarted when it is
idle, and only said to run older code while it is busy; so is a server of an
older protocol, as an installed ZERO runs until its first update past it:
protocol 1 names a request's message in "verb", 2 knows host/hello, 3 refuses
a hello naming another protocol and answers one naming none.
"""

import fcntl
import shutil
import subprocess
import sys
import time
from pathlib import Path

from client.connection import NoAnswer, NotRunning, ProtocolError, request
from protocol import messages, version

# One hello's wait; a server still starting gets as long as a start
PROBE_SECONDS = 2
START_SECONDS = 10
POLL_SECONDS = 0.05
# Protocol 2's hello and restart (ZERO's server was its host until 0.6.2.8),
# then protocol 1's, for a server that runs one still
HOST_HELLO = {"message": "host/hello"}
HOST_RESTART = {"message": "host/stop", "force": False, "reason": "restart"}
OLD_HELLO = {"verb": "hello"}
OLD_RESTART = {"verb": "shutdown", "reason": "restart"}
# Their replies' fields, under the names protocol 3 gives them
OLD_FIELDS = {"busy": "busy_with", "started": "started_at", "last": "last_session"}
# The server's own entry, so a client never loads the server's code
SERVER_SCRIPT = Path(__file__).resolve().parents[1] / "server" / "main.py"
# The server's Python: this one, isolated from the machine's Python settings,
# which are the user's (paths.own_python, which a client doesn't import)
SERVER_PYTHON = (sys.executable, "-I")


class Hung(Exception):
    """A server holds the socket but doesn't answer."""


def hello(paths, timeout=PROBE_SECONDS):
    """The running server's hello, or None when no server answers the socket.
    Raises NoAnswer for a server that took the connection and said nothing."""
    payload = messages.request("server/hello", protocol=version.PROTOCOL)
    try:
        reply = request(paths.socket, payload, timeout)
        if not reply.get("ok") and reply.get("protocol", version.PROTOCOL) < (
                version.PROTOCOL):
            # An older server of protocol 3 or later refuses a hello naming
            # another protocol; one naming none gets its whole reply
            reply = request(paths.socket, messages.request("server/hello"),
                            timeout)
        elif not reply.get("ok") and "protocol" not in reply:
            # An older server knows no server/hello; its own hello tells
            for old_hello, number in ((HOST_HELLO, 2), (OLD_HELLO, 1)):
                old = request(paths.socket, old_hello, timeout)
                if old.get("ok"):
                    return {**renamed(old),
                            "protocol": old.get("protocol", number)}
    except NoAnswer:
        raise
    except NotRunning:
        return None
    if not reply.get("ok"):
        raise ProtocolError(reply.get("error", "hello refused"))
    return reply


def renamed(reply):
    """An older server's reply under protocol 3's names."""
    return {OLD_FIELDS.get(name, name): value for name, value in reply.items()}


def server_pid(paths):
    """The pid server.lock names, for a server that doesn't answer."""
    try:
        return paths.lock.read_text().strip() or "?"
    except OSError:
        return "?"


def busy_text(busy):
    """'1 turn and 2 jobs are running', from busy_with()'s reasons."""
    parts = []
    for name, one, many in (("turns", "turn", "turns"),
                            ("queued", "queued message", "queued messages"),
                            ("jobs", "job", "jobs")):
        count = len(busy.get(name, ()))
        if count:
            parts.append(f"{count} {one if count == 1 else many}")
    if not parts:
        return "nothing is running"
    text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    total = sum(len(busy.get(n, ())) for n in ("turns", "queued", "jobs"))
    return f"{text} {'is' if total == 1 else 'are'} running"


def probe(paths, seconds=START_SECONDS):
    """hello, giving a server that takes connections but is still starting (its
    boot repair) up to `seconds` to answer."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            return hello(paths)
        except NoAnswer:
            if time.monotonic() >= deadline:
                raise Hung(
                    f"ZERO's server (pid {server_pid(paths)}) doesn't answer; "
                    "stop it with: zero server stop --force"
                ) from None


def ensure_server(paths, unit=None, command=None, popen=subprocess.Popen,
                  run=subprocess.run, systemd=None, seconds=START_SECONDS,
                  report=None, kept_older=None):
    """Make sure a server of this code answers. `unit`: the systemd unit's name,
    the home's own when None (tests pass a throwaway one). `command`: the
    server's argv (tests: a scripted server). `systemd`: True or False to choose,
    None to ask the user's systemd. `kept_older()`: called when a busy server
    of older code stays until it is idle (the full screen says so)."""
    report = report or (lambda text: print(text, file=sys.stderr))
    unit = unit or paths.unit
    reply = probe(paths, seconds)
    if reply is not None and server_is_current(paths, reply, report, seconds,
                                               kept_older):
        return reply
    paths.make()
    with StartLock(paths, seconds):
        # Another terminal may have started one while this one waited
        reply = probe(paths, seconds)
        if reply is not None:
            return reply
        argv = command if command is not None else [*SERVER_PYTHON, str(SERVER_SCRIPT)]
        if systemd is None:
            systemd = systemd_usable(run)
        return start(paths, unit, argv, popen, run, systemd, seconds, report)


def shutdown(paths, timeout=5, protocol=None, **fields):
    """Ask the server to stop (`force`, `reason`: server/stop's fields): its
    reply, stopping or what keeps it busy. `protocol`: the server's, from
    its hello, which is asked for when not given: a protocol 2 server stops
    on host/stop."""
    if protocol is None:
        found = hello(paths, timeout)
        protocol = found["protocol"] if found is not None else version.PROTOCOL
    if protocol == 2:
        return renamed(
            request(paths.socket, {"message": "host/stop", **fields}, timeout)
        )
    return request(paths.socket, messages.request("server/stop", **fields), timeout)


# Said when a server of older code stays, busy, until it is idle
KEPT_OLDER = "ZERO's server runs older code; it is replaced once it is idle."


def server_is_current(paths, reply, report, seconds, kept_older=None):
    """True if the server runs this code, or is busy and stays for now; False
    once an idle server of another build has gone, so a fresh one starts.
    `kept_older()`: called when an older one stays."""
    kept_older = kept_older or (lambda: None)
    old = reply["protocol"] < version.PROTOCOL
    if reply["protocol"] != version.PROTOCOL and (not old or reply.get("busy_with")):
        raise ProtocolError(
            f"ZERO's server speaks protocol {reply['protocol']}, this zero "
            f"{version.PROTOCOL}"
            + (": it is replaced once it is idle, or stop it now with "
               "zero server stop --force" if old else
               ": restart it with zero server stop --force")
        )
    if reply["build"] == version.build_hash():
        return True
    if reply.get("busy_with"):
        report(KEPT_OLDER)
        kept_older()
        return True
    if reply["protocol"] == 1:
        stopping = request(paths.socket, OLD_RESTART, PROBE_SECONDS)
    elif reply["protocol"] == 2:
        stopping = renamed(request(paths.socket, HOST_RESTART, PROBE_SECONDS))
    else:
        stopping = shutdown(paths, PROBE_SECONDS, protocol=reply["protocol"],
                            reason="restart")
    if not stopping.get("stopping"):
        # It got busy in the meantime
        report(KEPT_OLDER)
        kept_older()
        return True
    report("ZERO's server runs older code: restarting it.")
    deadline = time.monotonic() + seconds
    while hello(paths) is not None:
        if time.monotonic() >= deadline:
            raise NotRunning("ZERO's old server did not exit")
        time.sleep(POLL_SECONDS)
    return False


class StartLock:
    """start.lock, held while a client starts a server, waited for up to
    `seconds`."""

    def __init__(self, paths, seconds):
        self.paths = paths
        self.seconds = seconds
        self.file = None

    def __enter__(self):
        self.file = open(self.paths.start_lock, "a+")
        deadline = time.monotonic() + self.seconds
        while True:
            try:
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    self.file.close()
                    raise NotRunning(
                        "another zero has been starting ZERO's server for "
                        f"{self.seconds} s; try again"
                    ) from None
                time.sleep(POLL_SECONDS)

    def __exit__(self, *args):
        self.file.close()


def systemd_usable(run):
    """Whether the user's systemd can start a unit."""
    if not shutil.which("systemd-run") or not shutil.which("systemctl"):
        return False
    try:
        result = run(["systemctl", "--user", "show", "--property=Version"],
                     capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def unit_command(paths, unit, argv):
    """systemd-run's argv: a cgroup of its own, so a dead server's jobs die with
    it; the client's folders passed on, since the user manager's environment,
    not the terminal's, is what the server gets."""
    setenv = [f"--setenv={k}={v}" for k, v in paths.environment().items()]
    # The server tells its own commands from the user's clients by its unit
    # (server/peers.py)
    setenv.append(f"--setenv=ZERO_SERVER_UNIT={unit}")
    return [
        "systemd-run", "--user", f"--unit={unit}", "--collect", "--quiet",
        "--description=ZERO's server", "-p", "KillMode=mixed",
        f"--working-directory={paths.state}", *setenv, "--", *argv,
    ]


def start(paths, unit, argv, popen, run, systemd, seconds, report):
    deadline = time.monotonic() + seconds
    if systemd:
        how = f"a systemd user unit, {unit} (log: journalctl --user -u {unit})"
        # An old unit of that name still stopping: wait and try again
        while True:
            result = run(unit_command(paths, unit, argv), capture_output=True,
                         text=True, timeout=seconds)
            if result.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise NotRunning(
                    f"ZERO's server could not start as {unit}: "
                    f"{result.stderr.strip()}"
                )
            time.sleep(0.2)
    else:
        how = f"a detached process (log: {paths.server_log})"
        try:
            with open(paths.server_log, "ab") as output:
                popen(argv, stdin=subprocess.DEVNULL, stdout=output,
                      stderr=subprocess.STDOUT, start_new_session=True,
                      cwd=paths.state)
        except (OSError, subprocess.SubprocessError) as error:
            raise NotRunning(f"ZERO's server could not start: {error}") from error
    while time.monotonic() < deadline:
        try:
            reply = hello(paths, timeout=0.5)
        except NoAnswer:
            reply = None
        if reply is not None:
            report(f"Started ZERO's server as {how} (pid {reply['pid']}).")
            return reply
        time.sleep(POLL_SECONDS)
    raise NotRunning(f"ZERO's server did not answer after starting as {how}")
