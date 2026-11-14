"""A ZERO installed from a git clone moves to its own Python on its first
start.

Before 7.5.3 ZERO was a git clone with a .venv on the machine's Python, and
its `zero update` only checks out code: a clone's update to this release
ends in this code, which then moves the install. This release's archive for
the machine, from where the clone's origin publishes, is fetched, tried and
switched to in the install folder (install_folder.py); the zero command that
ran the clone is rewritten to run it; and the command that started this is
run again there. A move that fails leaves the clone as it was, running, and
is tried again an hour later, or at once by `zero update`. The clone is left
behind until the next switch removes it, once it is clean and nothing runs
from it. Kept while a clone of an older release may still update to a newer
one: its own update checks out the newest tag, whose code moves it.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from apps.cli import releases
from apps.cli.install_folder import Busy, InstallError, InstallFolder
from paths import LAUNCHER_READ_BYTES, NAME, changelog_version

# A failed move's wait before the next start tries again
RETRY_SECONDS = 3600


def installed_clone(code, which=shutil.which):
    """The zero command that runs this clone when it is an installed one
    (install.sh's .venv runs this process, a zero command names its
    main.py); else None: a tree, a release, a clone run by hand."""
    code = Path(code)
    if not (code / ".git").exists():
        return None
    venv = code / ".venv"
    if Path(sys.prefix).resolve() != venv.resolve():
        return None
    main = str(code / "apps" / "cli" / "main.py")
    found = which(NAME)
    candidates = [Path(found)] if found else []
    candidates.append(Path.home() / ".local" / "bin" / NAME)
    for command in candidates:
        try:
            with open(command, errors="replace") as stream:
                if main in stream.read(LAUNCHER_READ_BYTES):
                    return command
        except OSError:
            continue
    return None


def origin(code):
    """The clone's origin, or None."""
    try:
        done = subprocess.run(
            ["git", "-C", str(code), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def due(folder, now):
    """Whether a move may be tried: none failed in the last hour."""
    failed = folder.state().get("move_failed_at")
    return not isinstance(failed, (int, float)) or now - failed >= RETRY_SECONDS


def move(code, command, folder, version, out):
    """The clone's install moved to `version`'s archive in `folder`: the
    zero command rewritten. InstallError, Busy and ReleaseError say why
    not, the clone untouched."""
    place = releases.source(releases.source_from_origin(origin(code) or ""))
    if place is None:
        raise InstallError(f"its origin ({origin(code) or 'none'}) isn't on "
                           "GitHub, and ZERO_RELEASES_URL names no mirror")
    with folder.locked():
        folder.sweep()
        out(f"ZERO {version} moves to its own Python (free-threaded), "
            "so it needs none of this machine's.")
        # A move killed after its switch, before the command was written,
        # has the release in place already
        if folder.current_version() != version:
            archive = releases.fetch(place, version, folder.scratch(), out)
            folder.add(folder.unpack(archive), version, out)
        folder.leave_behind(code)
        state = folder.state()
        state.pop("move_failed_at", None)
        folder.write_state(state)
        folder.write_command(command.parent)
    out(f"ZERO now runs from {folder.release(version)}; {command} runs it. "
        f"The clone in {code} is no longer used: the next update removes it.")


def run(code, argv, out=None, now=time.time, execv=os.execv, which=shutil.which):
    """On an installed clone's start: the move, if due, then `argv` run again
    on the new install; nothing when there is nothing to move, or when the
    move failed (said once, the clone running on)."""
    out = out or (lambda text: print(text, file=sys.stderr))
    command = installed_clone(code, which)
    version = changelog_version(Path(code) / "CHANGELOG.md")
    if command is None or version is None:
        return False
    folder = InstallFolder()
    if not due(folder, now()):
        return False
    try:
        move(code, command, folder, version, out)
    except Busy:
        return False
    # Whatever stops it, the clone runs on: this is the start of the user's
    # command, run on the clone's Python (3.11 and up)
    except Exception as error:
        try:
            folder.write_state({**folder.state(), "move_failed_at": now()})
        except OSError:
            pass
        out(f"ZERO couldn't move to its own Python: {error}. It runs from "
            f"{code} as before; the move is tried again in an hour, or now "
            "with zero update.")
        return False
    execv(str(command), [str(command), *argv])
    return True
