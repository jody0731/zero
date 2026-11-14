"""Find ZERO's HTTP door, or start it: a knock on its port says whether
something listens there (systemd's socket counts: the knock starts the door).
Where nothing does, the door starts as a transient systemd user unit (a cgroup
of its own, so ZERO's server never takes it for one of its own commands),
else as a detached process; a lock keeps two clients from starting two.
"""

import fcntl
import http.client
import socket
import subprocess
import time

from apps.http_door import guard
from apps.http_door.systemd_units import door_command, systemd_usable
from client.connection import NotRunning, read_token

# How long a start may take before the door answers
START_SECONDS = 10
POLL_SECONDS = 0.05
KNOCK_SECONDS = 1.0


def url(paths):
    return f"http://127.0.0.1:{paths.http_door_port}"


def knock(port, seconds=KNOCK_SECONDS):
    """Whether something takes connections on 127.0.0.1:`port`."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=seconds):
            return True
    except OSError:
        return False


def answers(paths, seconds=2.0):
    """Whether ZERO's door answers there (its own page, signed out), not
    another program on the same port."""
    # http.client, not urllib: no proxy of the environment's in between
    conn = http.client.HTTPConnection("127.0.0.1", paths.http_door_port,
                                      timeout=seconds)
    try:
        conn.request("GET", "/ui/style.css")
        reply = conn.getresponse()
        reply.read()
        return (reply.getheader("Server") or "").startswith("ZERO")
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def start_command(paths, unit, command):
    """systemd-run's argv for the door: a unit of its own."""
    setenv = [f"--setenv={k}={v}" for k, v in paths.environment().items()]
    return ["systemd-run", "--user", f"--unit={unit}", "--collect", "--quiet",
            f"--description=ZERO's HTTP door, for {paths.home}", *setenv,
            "--", *command]


def ensure_door(paths, command=None, popen=subprocess.Popen,
                run=subprocess.run, systemd=None, seconds=START_SECONDS):
    """The door's URL once something answers on its port: started here when
    nothing does. NotRunning when it doesn't start, or the port is another
    program's."""
    port = paths.http_door_port
    if knock(port):
        if answers(paths):
            return url(paths)
        raise NotRunning(f"127.0.0.1:{port} is another program's; set "
                         "[http_door] port in config.toml to another")
    command = command or door_command()
    paths.make()
    with open(paths.run / "http-door-start.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if knock(port):
            return url(paths)
        if systemd is None:
            systemd = systemd_usable(run)
        # A name of its own, beside the installed units' (when the socket
        # is off)
        unit = f"{paths.http_door_unit}-on-demand"
        log = paths.state / "http-door.log"
        if systemd:
            result = run(start_command(paths, unit, command),
                         capture_output=True, text=True, timeout=seconds)
            systemd = result.returncode == 0
        if not systemd:
            with open(log, "ab") as output:
                popen(command, stdin=subprocess.DEVNULL, stdout=output,
                      stderr=subprocess.STDOUT, start_new_session=True,
                      env=None)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if knock(port, 0.2):
                return url(paths)
            time.sleep(POLL_SECONDS)
    raise NotRunning(f"ZERO's HTTP door didn't answer on 127.0.0.1:{port}")


def sign_in_url(paths):
    """The page's one-use sign-in link (guard.py), from the app token."""
    token = read_token(paths.app_token)
    if token is None:
        raise NotRunning(f"no app token at {paths.app_token}: ZERO's server "
                         "makes it when it starts")
    return f"{url(paths)}/?sign_in={guard.sign_in_code(token)}"
