"""ZERO's install folder: a folder for each release, `current` naming the one
that runs, and the zero command that runs it (Codex's standalone install,
read 14 Nov: the shared notes' 7.5.3-prior-art.md).

    <install folder>/          ~/.local/share/zero unless the user says
      current -> versions/0.7.5.3
      versions/0.7.5.3/        a release archive, unpacked: python/, zero/,
                               bin/zero, release.json
      .incoming/               what a switch fetches, unpacks and tries
      .lock                    held while a switch runs
      install.json             a clone a move left behind

Every change is a rename. A release is unpacked and tried in .incoming/,
renamed into versions/, then `current` is replaced in one step, so the zero
command finds the old release or the new, and a failure before the rename
leaves the old one running. Tried means its own command answers with its
version and its Python loads the server's code with the GIL off. At each
switch a release that is neither current nor the one switched to, and that
no process runs from, is removed, so the one before stays until the next
switch; so is a clone a move left behind, once it is clean. .incoming/ is
emptied when a switch starts: what a killed one left.
"""

import fcntl
import json
import os
import secrets
import shlex
import shutil
import subprocess
import tarfile
from contextlib import contextmanager
from pathlib import Path

from paths import (
    CODE_FOLDER, CURRENT, NAME, PYTHON_FOLDER, RELEASE_FILE, release_command,
    sync_folder, write_whole,
)

VERSIONS = "versions"
INCOMING = ".incoming"
LOCK = ".lock"
STATE = "install.json"
# A tried release's own answers, each given this long
TRY_SECONDS = 120
# What a release's Python loads to show it can run ZERO's server: the
# assembly (every component and its packages) and the server's entry
TRY_IMPORTS = (
    "import sys; sys.path.insert(0, sys.argv[1]); import assembly, server.main; "
    "print('gil', getattr(sys, '_is_gil_enabled', lambda: True)())"
)
# A release's own command: its code on its own Python, isolated from the
# machine's Python settings (paths.own_python), from wherever it is linked
RELEASE_COMMAND = f"""#!/bin/sh
# ZERO: this release's code on its own Python
here=$(cd "$(dirname "$0")/.." && pwd -P)
exec "$here/{PYTHON_FOLDER}/bin/python3" -I "$here/{CODE_FOLDER}/apps/cli/main.py" "$@"
"""


class Busy(Exception):
    """Another zero holds the install folder's lock."""


class InstallError(Exception):
    """A switch stopped: the text says why; the release that ran still does."""


def default_folder():
    """Where releases are installed: $XDG_DATA_HOME/zero, else
    ~/.local/share/zero."""
    data = os.environ.get("XDG_DATA_HOME")
    base = Path(data) if data else Path.home() / ".local" / "share"
    return base / NAME


def default_bin():
    return Path.home() / ".local" / "bin"


def command_text(folder):
    """The zero command on the PATH: the install folder's current release."""
    current = Path(folder) / CURRENT / "bin" / NAME
    return ("#!/bin/sh\n# ZERO's command: its install folder's current release\n"
            f'exec {shlex.quote(str(current))} "$@"\n')


def release_file(folder):
    """A release folder's release.json, read: {} when it can't be."""
    try:
        found = json.loads((Path(folder) / RELEASE_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def try_release(folder, version):
    """None when the release in `folder` runs as `version` with the GIL off,
    else what is wrong with it."""
    folder = Path(folder)
    try:
        said = subprocess.run(
            [str(release_command(folder)), "--version"], capture_output=True,
            text=True, timeout=TRY_SECONDS, stdin=subprocess.DEVNULL,
        )
        if said.returncode or said.stdout.split() != [NAME, version]:
            return (f"its zero --version said {said.stdout.strip()!r} "
                    f"{said.stderr.strip()[-300:]!r}, not {NAME} {version}")
        loaded = subprocess.run(
            [str(folder / PYTHON_FOLDER / "bin" / "python3"), "-I", "-c",
             TRY_IMPORTS, str(folder / CODE_FOLDER)],
            capture_output=True, text=True, timeout=TRY_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"it didn't run: {error}"
    if loaded.returncode:
        return f"its Python couldn't load ZERO's server: {loaded.stderr[-300:]}"
    if loaded.stdout.split() != ["gil", "False"]:
        return f"its Python runs with the GIL on ({loaded.stdout.strip()})"
    return None


def running_from(folder, proc=Path("/proc"), own=None):
    """The pids of processes running from `folder` (their program, or a path
    among their arguments, inside it), less this one. Only the user's own
    processes can run from the user's folders, and those /proc always
    shows; with no /proc, every folder counts as in use (None)."""
    own = os.getpid() if own is None else own
    inside = os.path.realpath(folder) + os.sep
    if not proc.is_dir():
        return None
    found = []
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == own:
            continue
        try:
            program = os.readlink(entry / "exe")
        except OSError:
            program = ""
        try:
            arguments = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            arguments = []
        paths = [program] + [os.fsdecode(a) for a in arguments if a]
        if any(os.path.realpath(p).startswith(inside) for p in paths
               if p.startswith(os.sep)):
            found.append(int(entry.name))
    return found


def clone_is_clean(folder):
    """Whether a clone a move left behind holds nothing but ZERO's tracked
    files, its .venv and Python's caches (git's word, ignored files too)."""
    folder = Path(folder)
    if not (folder / ".git").exists() or not (folder / "apps" / "cli").is_dir():
        return False
    try:
        done = subprocess.run(
            ["git", "-C", str(folder), "status", "--porcelain", "--ignored"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if done.returncode:
        return False
    for line in done.stdout.splitlines():
        parts = Path(line[3:].rstrip("/")).parts
        if not (line.startswith("!! ") and parts and (
                parts[0] == ".venv" or parts[-1] == "__pycache__"
                or only_caches(folder.joinpath(*parts)))):
            return False
    return True


def only_caches(folder):
    """Whether a folder holds nothing but Python's caches (one an older
    release's code left, its sources gone)."""
    if not folder.is_dir() or folder.is_symlink():
        return False
    return all("__pycache__" in path.relative_to(folder).parts
               for path in folder.rglob("*") if not path.is_dir())


class InstallFolder:
    def __init__(self, path=None):
        self.path = Path(path) if path else default_folder()
        self.versions = self.path / VERSIONS
        self.current = self.path / CURRENT
        self.incoming = self.path / INCOMING

    def release(self, version):
        return self.versions / version

    def current_version(self):
        """The release `current` names, or None."""
        try:
            target = os.readlink(self.current)
        except OSError:
            return None
        return Path(target).name

    @contextmanager
    def locked(self):
        """The folder's lock, held for a switch; Busy when another zero holds
        it (a second terminal's move, an update)."""
        self.path.mkdir(parents=True, exist_ok=True)
        handle = os.open(self.path / LOCK, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Busy(f"another zero is installing or updating ZERO in "
                           f"{self.path}") from None
            yield self
        finally:
            os.close(handle)

    def sweep(self, keep=None):
        """Empty .incoming/ of what a killed switch left, less `keep` (the
        folder the running code is in, when it runs from there)."""
        if not self.incoming.is_dir():
            return
        keep = os.path.realpath(keep) + os.sep if keep else None
        for entry in self.incoming.iterdir():
            if keep and keep.startswith(os.path.realpath(entry) + os.sep):
                continue
            remove(entry)

    def scratch(self):
        """A new folder in .incoming/."""
        folder = self.incoming / secrets.token_hex(4)
        folder.mkdir(parents=True)
        return folder

    def unpack(self, archive):
        """The archive unpacked into a folder of .incoming/: its release
        folder (the archive's one top folder, holding release.json)."""
        into = self.scratch()
        try:
            with tarfile.open(archive) as tar:
                tar.extractall(into, filter="data")
        except (OSError, tarfile.TarError) as error:
            raise InstallError(f"{Path(archive).name} didn't unpack: "
                               f"{error}") from None
        found = [entry for entry in into.iterdir()]
        if len(found) != 1 or not (found[0] / RELEASE_FILE).is_file():
            raise InstallError(f"{Path(archive).name} isn't a release archive "
                               f"of ZERO (no one folder with {RELEASE_FILE})")
        return found[0]

    def copy_in(self, folder):
        """A copy of a release folder, made in .incoming/."""
        into = self.scratch() / Path(folder).name
        shutil.copytree(folder, into, symlinks=True)
        return into

    def place(self, folder, version):
        """Rename a tried release folder into versions/; one already there
        (a switch that stopped after this step) is renamed aside first."""
        target = self.release(version)
        self.versions.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            if self.current_version() == version:
                raise InstallError(f"ZERO {version} is the one that runs")
            os.replace(target, self.scratch() / "replaced")
        os.replace(folder, target)
        sync_folder(self.versions)
        return target

    def tidy(self, keep, out=print):
        """Remove the releases not in `keep` that nothing runs from, and a
        clone a move left behind once it is clean and unused."""
        if self.versions.is_dir():
            for entry in sorted(self.versions.iterdir()):
                if entry.name in keep or running_from(entry) != []:
                    continue
                aside = self.scratch() / entry.name
                os.replace(entry, aside)
                remove(aside)
        state = self.state()
        kept = []
        for clone in state.get("left_behind", []):
            if not Path(clone).exists():
                continue
            if running_from(clone) == [] and clone_is_clean(clone):
                remove(Path(clone))
                out(f"Removed {clone}, the clone ZERO ran from before it had "
                    "its own Python.")
            else:
                kept.append(clone)
        if kept != state.get("left_behind", kept):
            self.write_state({**state, "left_behind": kept})

    def switch(self, version):
        """`current` replaced by a link to `version`, in one rename."""
        link = self.path / f".{CURRENT}.{secrets.token_hex(4)}"
        os.symlink(Path(VERSIONS) / version, link)
        try:
            os.replace(link, self.current)
        except OSError:
            link.unlink(missing_ok=True)
            raise
        sync_folder(self.path)

    def write_command(self, bin_folder):
        """The zero command in `bin_folder`, running the current release."""
        command = Path(bin_folder) / NAME
        write_whole(command, command_text(self.path))
        command.chmod(0o755)
        return command

    def state(self):
        try:
            found = json.loads((self.path / STATE).read_text())
        except (OSError, ValueError):
            return {}
        return found if isinstance(found, dict) else {}

    def write_state(self, state):
        write_whole(self.path / STATE, json.dumps(state, indent=2) + "\n")

    def leave_behind(self, clone):
        """Note a clone a move left behind, for a later switch to remove."""
        state = self.state()
        listed = state.get("left_behind", [])
        if str(clone) not in listed:
            self.write_state({**state, "left_behind": [*listed, str(clone)]})

    def add(self, unpacked, version, out=print):
        """A release unpacked in .incoming/ tried, placed, the rest tidied
        and switched to; InstallError when it can't run, nothing switched."""
        problem = try_release(unpacked, version)
        if problem:
            raise InstallError(f"ZERO {version} didn't run here: {problem}")
        self.place(unpacked, version)
        self.tidy({version, self.current_version()}, out)
        self.switch(version)
        self.sweep()


def remove(path):
    """A file, link or folder gone; what can't be removed stays."""
    path = Path(path)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except OSError:
            pass


def is_release_folder_in(folder, install):
    """Whether a release folder is one of `install`'s versions."""
    return Path(os.path.realpath(folder)).parent == Path(
        os.path.realpath(install.versions))

