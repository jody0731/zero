"""Find ZERO's host, or start one: the host on demand.

`hello` with a short timeout says whether a host runs. When none does, the
client takes start.lock, says hello again (another terminal may have just
started one) and only then starts it: as a transient systemd user unit,
`zero-host`, where the user's systemd answers, else as a detached process. It
polls until the host answers. A host that takes the connection but never
answers is reported with its pid, never replaced by a second one. A host with
another build (the code changed since it started) is restarted when it is
idle, and only said to run older code while it is busy.
"""

import fcntl
import shutil
import subprocess
import sys
import time
from pathlib import Path

from client.connection import NoAnswer, NotRunning, ProtocolError, request
from protocol import verbs, version

UNIT = "zero-host"
# One hello's wait; a host still starting gets as long as a start
PROBE_SECONDS = 2
START_SECONDS = 10
POLL_SECONDS = 0.05
# The host's own entry, so a client never loads the host's code
SCRIPT = Path(__file__).resolve().parents[1] / "host" / "main.py"


class Hung(Exception):
    """A host holds the socket but doesn't answer."""


def hello(paths, timeout=PROBE_SECONDS):
    """The running host's hello, or None when no host answers the socket.
    Raises NoAnswer for a host that took the connection and said nothing."""
    try:
        reply = request(
            paths.socket, verbs.request("hello", protocol=version.PROTOCOL), timeout
        )
    except NoAnswer:
        raise
    except NotRunning:
        return None
    if not reply.get("ok"):
        raise ProtocolError(reply.get("error", "hello refused"))
    return reply


def host_pid(paths):
    """The pid host.lock names, for a host that doesn't answer."""
    try:
        return paths.lock.read_text().strip() or "?"
    except OSError:
        return "?"


def busy_text(busy):
    """'1 turn and 2 jobs are running', from busy()'s reasons."""
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
    """hello, giving a host that takes connections but is still starting (its
    boot repair) up to `seconds` to answer."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            return hello(paths)
        except NoAnswer:
            if time.monotonic() >= deadline:
                raise Hung(
                    f"ZERO's host (pid {host_pid(paths)}) doesn't answer; "
                    "stop it with: zero host stop --force"
                ) from None


def ensure_host(paths, unit=UNIT, command=None, popen=subprocess.Popen,
                run=subprocess.run, systemd=None, seconds=START_SECONDS,
                report=None):
    """Make sure a host of this code answers. `unit`: the systemd unit's name
    (tests pass a throwaway one). `command`: the host's argv (tests: a scripted
    host). `systemd`: True or False to choose, None to ask the user's systemd."""
    report = report or (lambda text: print(text, file=sys.stderr))
    reply = probe(paths, seconds)
    if reply is not None and current(paths, reply, report, seconds):
        return reply
    paths.make()
    with start_lock(paths, seconds):
        # Another terminal may have started one while this one waited
        reply = probe(paths, seconds)
        if reply is not None:
            return reply
        argv = command if command is not None else [sys.executable, str(SCRIPT)]
        if systemd is None:
            systemd = systemd_answers(run)
        return start(paths, unit, argv, popen, run, systemd, seconds, report)


def shutdown(paths, timeout=5, **fields):
    """Ask the host to stop (`force`, `reason`: the shutdown verb's): its
    reply, stopping or what keeps it busy."""
    return request(paths.socket, verbs.request("shutdown", **fields), timeout)


def current(paths, reply, report, seconds):
    """True if the host runs this code, or is busy and stays for now; False
    once an idle host of another build has gone, so a fresh one starts."""
    if reply["protocol"] != version.PROTOCOL:
        raise ProtocolError(
            f"ZERO's host speaks protocol {reply['protocol']}, this zero "
            f"{version.PROTOCOL}: restart it with zero host stop"
        )
    if reply["build"] == version.build():
        return True
    if reply.get("busy"):
        report("ZERO's host runs older code; it is replaced once it is idle.")
        return True
    stopping = shutdown(paths, PROBE_SECONDS, reason="restart")
    if not stopping.get("stopping"):
        # It got busy in the meantime
        report("ZERO's host runs older code; it is replaced once it is idle.")
        return True
    report("ZERO's host runs older code: restarting it.")
    deadline = time.monotonic() + seconds
    while hello(paths) is not None:
        if time.monotonic() >= deadline:
            raise NotRunning("ZERO's old host did not exit")
        time.sleep(POLL_SECONDS)
    return False


class start_lock:
    """start.lock, held while a client starts a host, waited for up to
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
                        "another zero has been starting ZERO's host for "
                        f"{self.seconds} s; try again"
                    ) from None
                time.sleep(POLL_SECONDS)

    def __exit__(self, *args):
        self.file.close()


def systemd_answers(run):
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
    """systemd-run's argv: a cgroup of its own, so a dead host's jobs die with
    it; the client's folders passed on, since the user manager's environment,
    not the terminal's, is what the host gets."""
    setenv = [f"--setenv={k}={v}" for k, v in paths.environment().items()]
    # The host tells its own commands from the user's clients by its unit
    # (host/peers.py)
    setenv.append(f"--setenv=ZERO_HOST_UNIT={unit}")
    return [
        "systemd-run", "--user", f"--unit={unit}", "--collect", "--quiet",
        "--description=ZERO's host", "-p", "KillMode=mixed",
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
                    f"ZERO's host could not start as {unit}: "
                    f"{result.stderr.strip()}"
                )
            time.sleep(0.2)
    else:
        how = f"a detached process (log: {paths.host_log})"
        try:
            with open(paths.host_log, "ab") as output:
                popen(argv, stdin=subprocess.DEVNULL, stdout=output,
                      stderr=subprocess.STDOUT, start_new_session=True,
                      cwd=paths.state)
        except (OSError, subprocess.SubprocessError) as error:
            raise NotRunning(f"ZERO's host could not start: {error}") from error
    while time.monotonic() < deadline:
        try:
            reply = hello(paths, timeout=0.5)
        except NoAnswer:
            reply = None
        if reply is not None:
            report(f"Started ZERO's host as {how} (pid {reply['pid']}).")
            return reply
        time.sleep(POLL_SECONDS)
    raise NotRunning(f"ZERO's host did not answer after starting as {how}")
