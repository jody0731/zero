"""The door's port, held by systemd: a user socket unit that listens on
127.0.0.1:PORT from login, with no ZERO process running, and starts the
door's service at the first connection, handing it the listening socket
(Accept=no, LISTEN_FDS: Podman's `podman.socket`, Docker's, PipeWire's
way). The door leaves when idle; the socket stays and starts it again.

    ~/.config/systemd/user/zero-http-door.socket    (zero-http-door-<mark>
    ~/.config/systemd/user/zero-http-door.service    for another home)

Written by `zero install` and by a clone's move to its first release, and
by hand with `zero http-door install`; `zero http-door uninstall` and
`zero uninstall` take them away. Each file is written whole; the units name
the home they serve. On macOS (launchd's Sockets) and Windows these are
7.10's.
"""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from paths import (
    CURRENT, NAME, own_python, release_folder, systemd_user_folder, write_whole,
)

DESCRIPTION = "ZERO's HTTP door"

SOCKET_TEXT = """\
# Written by ZERO (zero http-door install): ZERO's HTTP door's port, held
# by systemd from login; the first connection starts the door
[Unit]
Description={description}: its port, for {home}

[Socket]
ListenStream=127.0.0.1:{port}
Accept=no

[Install]
WantedBy=sockets.target
"""

SERVICE_TEXT = """\
# Written by ZERO (zero http-door install): started by {unit}.socket at the
# first connection; it leaves when idle and the socket starts it again
[Unit]
Description={description}, for {home}
Requires={unit}.socket
After={unit}.socket

[Service]
ExecStart={command}
{environment}"""


def config_folder(environ=os.environ):
    """The user's systemd unit folder."""
    return systemd_user_folder(environ)


def door_command(code=None):
    """The command that runs this copy's door: an installed release's through
    its install folder's `current`, so an update needs no new unit; else
    this Python and this copy's command line."""
    code = Path(code) if code else Path(__file__).resolve().parents[2]
    release = release_folder(code)
    if release is not None:
        install = release.parent.parent
        current = install / CURRENT / "bin" / NAME
        if (install / CURRENT).exists():
            return [str(current), "http-door", "serve"]
        return [str(release / "bin" / NAME), "http-door", "serve"]
    return [*own_python(), str(code / "apps" / "cli" / "main.py"),
            "http-door", "serve"]


def unit_escaped(text):
    """A value as a unit file takes it: % starts systemd's specifiers."""
    return text.replace("%", "%%")


def texts(paths, command):
    """{file name: text} of the two units for this home."""
    unit = paths.http_door_unit
    home = unit_escaped(str(paths.home))
    environment = "".join(
        f"Environment={unit_escaped(shlex.quote(f'{name}={value}'))}\n"
        for name, value in paths.environment().items())
    command = unit_escaped(shlex.join(command))
    return {
        f"{unit}.socket": SOCKET_TEXT.format(
            description=DESCRIPTION, home=home, port=paths.http_door_port),
        f"{unit}.service": SERVICE_TEXT.format(
            description=DESCRIPTION, home=home, unit=unit,
            command=command, environment=environment),
    }


def systemd_usable(run=subprocess.run):
    """Whether the user's systemd answers."""
    if not shutil.which("systemctl"):
        return False
    try:
        result = run(["systemctl", "--user", "show", "--property=Version"],
                     capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def systemctl(*args, run=subprocess.run):
    """systemctl --user ARGS: (ok, what it said)."""
    try:
        result = run(["systemctl", "--user", *args], capture_output=True,
                     text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)
    said = (result.stderr or result.stdout or "").strip()
    return result.returncode == 0, said


def install(paths, command=None, folder=None, run=subprocess.run):
    """The units written (when missing or changed) and the socket enabled
    and listening: (ok, a line saying what happened)."""
    if not systemd_usable(run):
        return False, ("no user systemd here: zero web starts the door, and "
                       "agents use zero mcp serve")
    folder = Path(folder) if folder else config_folder()
    unit = paths.http_door_unit
    changed = False
    for name, text in texts(paths, command or door_command()).items():
        path = folder / name
        try:
            same = path.read_text() == text
        except OSError:
            same = False
        if not same:
            write_whole(path, text)
            changed = True
    if changed:
        ok, said = systemctl("daemon-reload", run=run)
        if not ok:
            return False, f"systemctl daemon-reload: {said}"
    ok, said = systemctl("enable", "--now", f"{unit}.socket", run=run)
    if not ok:
        return False, f"systemd couldn't hold 127.0.0.1:{paths.http_door_port}: {said}"
    # A changed service runs its new command from its next start
    if changed:
        systemctl("stop", f"{unit}.service", run=run)
    return True, (f"systemd holds 127.0.0.1:{paths.http_door_port} for ZERO's "
                  f"HTTP door ({unit}.socket in {folder})")


def uninstall(paths, folder=None, run=subprocess.run):
    """The socket stopped and disabled, the door stopped, the units removed:
    (ok, a line)."""
    folder = Path(folder) if folder else config_folder()
    unit = paths.http_door_unit
    files = [folder / f"{unit}.socket", folder / f"{unit}.service"]
    if not any(path.exists() for path in files):
        return True, f"no units of ZERO's HTTP door in {folder}"
    if systemd_usable(run):
        systemctl("disable", "--now", f"{unit}.socket", run=run)
        systemctl("stop", f"{unit}.service", run=run)
    for path in files:
        path.unlink(missing_ok=True)
    if systemd_usable(run):
        systemctl("daemon-reload", run=run)
    return True, f"removed {unit}.socket and {unit}.service from {folder}"


def held(paths, run=subprocess.run):
    """Whether systemd holds the door's port now (its socket listening)."""
    if not systemd_usable(run):
        return False
    ok, said = systemctl("is-active", f"{paths.http_door_unit}.socket", run=run)
    return ok and said == "active"
