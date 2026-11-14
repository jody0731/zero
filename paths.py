"""Where ZERO keeps its files: one home, ~/.zero ($ZERO_HOME moves it), so
the harness stays portable and deleting the home leaves a clean ZERO.

At the home's top are the user's files: config.toml (the settings,
settings.py), keys.toml (API keys), models.toml (the model servers they
added, model/), SOUL.md (who ZERO is), ZERO.md (the user, their machines,
their instructions and what ZERO remembers; both written from the templates
on first use), permissions.toml (the permissions they saved from an ask),
mcp_servers.toml (the MCP servers whose tools ZERO uses) and skills/ (ZERO's
own skills); all optional. state/ is the server's and lasts: the database, session logs,
trajectories, locks, jobs' output, the trash, server.log and crashes.log. run/
holds the running server's socket and locks. The sandbox hides the whole home
from commands. Each home's server runs as a systemd unit of its own: zero-server
for ~/.zero, zero-server-<hash> for any other, so two homes' servers run side by
side; so does its HTTP door (zero-http-door, on 127.0.0.1:4747 for ~/.zero).

Also the version this copy of ZERO is: its CHANGELOG.md, in ZERO's own
folder beside this file, has a section for each release, the newest at its
top. A copy installed from a release archive (7.5.3) sits in that release's
folder: its code in zero/, its own Python in python/, bin/zero running the
one on the other, release.json saying what it is (`release_folder`).

And the one way a file in the home is written (design rule 10): whole or not
at all (`write_whole`), and a change read, made and written back by one writer
at a time across ZERO's processes (`changing`). Each file ZERO reads back
states its format (`FILE_FORMAT_KEY`; the database's user_version, a log's
first record), and its reader takes what it knows of a newer one and says so
(`newer_note`).

Shared by the server and its clients, so neither imports the other. Read when
called, so a test's ZERO_HOME counts; nothing here names a user or a
machine.
"""

import fcntl
import hashlib
import os
import re
import shlex
import shutil
import sys
import tempfile
import tomllib
from contextlib import contextmanager
from pathlib import Path

NAME = "zero"
# The variable that moves the home
HOME_ENV = "ZERO_HOME"
# The longest path a Unix socket can bind to (sun_path, less its end)
SOCKET_MAX = 107
SOCKET = "zero.sock"
# The token file at the home's top: an app's proof of the user
APP_TOKEN = "app-token"
# How much of the zero on the PATH is read to see whether it runs this copy:
# a launcher is two lines
LAUNCHER_READ_BYTES = 4096
UNIT = f"{NAME}-server"
# ZERO's HTTP door (apps/http_door/): its systemd units' name for the usual
# home, and its port on 127.0.0.1 there; another home's port is offset by its
# hash (as its units are named), so a second home's door never answers for
# the first; config.toml's [http_door] port sets either
HTTP_DOOR_UNIT = f"{NAME}-http-door"
HTTP_DOOR_PORT = 4747
HTTP_DOOR_TABLE = "http_door"
# The ports another home's door is offset into, below 5000 (where other
# local servers sit) and the kernel's ephemeral range
HTTP_DOOR_PORTS_OTHER = 250

# A release's folder, unpacked from its archive: what it is (version,
# machine, where releases come from), ZERO's code, its own Python, and the
# command that runs the one on the other
RELEASE_FILE = "release.json"
CODE_FOLDER = "zero"
PYTHON_FOLDER = "python"
# The install folder's link to the release that runs (apps/cli/install_folder)
CURRENT = "current"

# A CHANGELOG section's heading: the version it is for
CHANGELOG_SECTION = re.compile(r"(?m)^## (\S+)")
# The line of a TOML file of ZERO's (and the key of a JSON one) that states
# the file's format
FILE_FORMAT_KEY = "file_format"


def process_umask():
    """The process's umask, read without setting it (Linux's status file);
    else set and set back, once, as the module loads, before any thread."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("Umask:"):
                return int(line.split()[1], 8)
    except (OSError, ValueError):
        pass
    mask = os.umask(0o077)
    os.umask(mask)
    return mask


# What a new file's mode loses, when its writer gives none
UMASK = process_umask()


def write_whole(path, data, mode=None):
    """Write a file whole or not at all: a draft beside it, synced, moved over
    it in one step, its folder synced; a reader sees the old file or the new,
    and a crash leaves the old. `data`: text, bytes, or pieces of bytes. The
    file keeps its mode; a new one takes `mode`, else the umask's. A link is
    written through: the file it points to is replaced, the link kept (a
    dotfiles repo's, or a dev home's link to the main home's keys)."""
    path = Path(os.path.realpath(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        kept = os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        kept = mode if mode is not None else 0o666 & ~UMASK
    handle, draft = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".draft",
                                     dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            if isinstance(data, str):
                stream.write(data.encode("utf-8"))
            elif isinstance(data, (bytes, bytearray)):
                stream.write(data)
            else:
                for piece in data:
                    stream.write(piece)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(draft, kept)
        os.replace(draft, path)
    except BaseException:
        try:
            os.unlink(draft)
        except FileNotFoundError:
            pass
        raise
    sync_folder(path.parent)


def sync_folder(folder):
    """Sync a folder, so a file moved into it lasts a power cut."""
    try:
        handle = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


@contextmanager
def changing(path):
    """Hold a file of ZERO's while it is read, changed and written back: one
    writer at a time, across ZERO's processes (a lock on its folder), so two
    changes made at once both land. A link's file is held where it is."""
    folder = Path(os.path.realpath(path)).parent
    folder.mkdir(parents=True, exist_ok=True)
    handle = os.open(folder, os.O_RDONLY)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)


def newer_note(where, found, known):
    """What a reader says of a file a newer ZERO wrote."""
    return (f"{where} was written by a newer ZERO (its format {found}; this one "
            f"knows {known}): what this ZERO knows of it is read, the rest left "
            "as it is")


def unknown_note(where, names):
    """What a reader says of lines it doesn't know."""
    listed = ", ".join(f"`{name}`" for name in sorted(names))
    return f"{where}: {listed} not known to this ZERO, left as it is"


def changelog_version(changelog=None):
    """The version a CHANGELOG's newest section is for, by default ZERO's own;
    None without one."""
    if changelog is None:
        changelog = Path(__file__).resolve().parent / "CHANGELOG.md"
    try:
        match = CHANGELOG_SECTION.search(changelog.read_text())
    except OSError:
        return None
    return match.group(1) if match else None


def release_folder(code=None):
    """The release folder this copy of ZERO runs from (its code's parent,
    with release.json), or None for a clone or a tree. `code`: ZERO's own
    folder, by default this one."""
    code = Path(code) if code else Path(__file__).resolve().parent
    folder = code.parent
    if code.name == CODE_FOLDER and (folder / RELEASE_FILE).is_file():
        return folder
    return None


def release_command(folder):
    """A release folder's own command: its code on its own Python."""
    return Path(folder) / "bin" / NAME


def own_python():
    """How ZERO starts a Python process of its own (its server, its full
    screen): this Python, isolated from the machine's Python settings
    (PYTHONPATH, PYTHONHOME, the user's site-packages), which are the
    user's, not ZERO's."""
    return [sys.executable, "-I"]


def zero_command(which=shutil.which):
    """How a command runs this copy of ZERO, for a shell: `zero` where the
    zero on the PATH runs it (its launcher names this copy's main.py, or the
    install folder's current release, this one), else a release's own
    command, else this Python and this copy's main.py, quoted. A command
    inherits the server's ZERO_HOME, so either reaches this server."""
    code = Path(__file__).resolve().parent
    main = code / "apps" / "cli" / "main.py"
    release = release_folder(code)
    found = which(NAME)
    if found:
        try:
            with open(found, errors="replace") as launcher:
                text = launcher.read(LAUNCHER_READ_BYTES)
        except OSError:
            text = ""
        if str(main) in text:
            return NAME
        if release is not None:
            current = release.parent.parent / CURRENT
            if (str(current) in text
                    and os.path.realpath(current) == str(release)):
                return NAME
    if release is not None:
        return shlex.quote(str(release_command(release)))
    return " ".join(shlex.quote(part) for part in [*own_python(), str(main)])


def usual_home():
    """The home when nothing moves it."""
    return Path.home() / f".{NAME}"


def home():
    """The home: ZERO_HOME's folder, made whole (a server started elsewhere,
    as its systemd unit, is led to the same one), else the usual."""
    base = os.environ.get(HOME_ENV)
    if not base:
        return usual_home()
    return Path(os.path.normpath(Path(base).expanduser().absolute()))


def soul_file():
    """Who ZERO is."""
    return home() / "SOUL.md"


def zero_md_file():
    """The user's instructions and what ZERO remembers."""
    return home() / "ZERO.md"


def keys_file():
    """The API keys, beside their environment variables (model/)."""
    return home() / "keys.toml"


def models_file():
    """The model servers the user added, and their changes to the ones ZERO
    ships with (model/)."""
    return home() / "models.toml"


def permissions_file():
    """The permissions the user saved from an ask."""
    return home() / "permissions.toml"


def mcp_servers_file():
    """The MCP servers whose tools ZERO uses, as the user listed them
    (mcp_servers/)."""
    return home() / "mcp_servers.toml"


def skills_folder():
    """ZERO's own skills, a folder each with its SKILL.md (skills/)."""
    return home() / "skills"


def shared_skills_folder():
    """The skills the agents on this machine share: the Agent Skills
    standard's folder in the user's home (skills/)."""
    return Path.home() / ".agents" / "skills"


def model_features_file():
    """What each model was measured to honour of its API's features
    (model/features.py): ZERO's own record, not the user's to edit. Named
    from the home alone: finding the socket's folder can fail, and has
    nothing to do with it."""
    return home() / "state" / "model_features.json"


def config_file():
    return home() / "config.toml"


def run_folder(top):
    """The running server's folder: run/ in the home, or, when a socket there
    would pass a Unix socket's limit, a folder named after the home in the
    login session's runtime folder (Hermes)."""
    run = top / "run"
    if len(str(run / SOCKET).encode()) <= SOCKET_MAX:
        return run
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        raise RuntimeError(
            f"ZERO's socket can't go in {run} (a Unix socket's path is at most "
            f"{SOCKET_MAX} bytes) and XDG_RUNTIME_DIR is not set"
        )
    tag = hashlib.sha256(str(top).encode()).hexdigest()[:12]
    return Path(runtime) / f"{NAME}-{tag}"


def home_tag(top):
    """A home's mark in its units' names: none for the usual home."""
    if Path(top).absolute() == usual_home().absolute():
        return None
    return hashlib.sha256(str(top).encode()).hexdigest()[:8]


def unit_name(top, unit=UNIT):
    """The systemd unit a home's server (or `unit`) runs as."""
    tag = home_tag(top)
    return unit if tag is None else f"{unit}-{tag}"


def http_door_port(top):
    """The port a home's HTTP door listens on, on 127.0.0.1: config.toml's
    [http_door] port, else the usual home's, offset by the home's mark for
    another home."""
    try:
        data = tomllib.loads((Path(top) / "config.toml").read_text())
        port = data.get(HTTP_DOOR_TABLE, {}).get("port")
    except (OSError, ValueError, AttributeError):
        port = None
    if type(port) is int and 0 < port < 65536:
        return port
    tag = home_tag(top)
    if tag is None:
        return HTTP_DOOR_PORT
    return HTTP_DOOR_PORT + 1 + int(tag, 16) % HTTP_DOOR_PORTS_OTHER


class Paths:
    def __init__(self, top=None):
        self.home = Path(top) if top else home()
        self.state = self.home / "state"
        self.db = self.state / "state.sqlite"
        self.sessions = self.state / "sessions"
        self.trajectories = self.state / "trajectories"
        self.locks = self.state / "locks"
        # Each job's output (jobs/), and deleted sessions (server/trash.py)
        self.jobs = self.state / "jobs"
        self.trash = self.state / "trash"
        # A detached server's output, and the CLI's crashes
        self.server_log = self.state / "server.log"
        self.crash_log = self.state / "crashes.log"
        self.run = run_folder(self.home)
        self.socket = self.run / SOCKET
        # Held by the server for its whole life: whoever holds it owns the socket
        self.lock = self.run / "server.lock"
        # The lock ZERO's server held before 0.6.2.8, when it was the host:
        # one still running holds it
        self.old_server_lock = self.run / "host.lock"
        # Held by a client while it starts a server, so two start one
        self.start_lock = self.run / "start.lock"
        # What proves the user where the socket can't tell who connects
        # (server/peers.py): made by the server on first use, kept across
        # restarts, readable by the user only
        self.app_token = self.home / APP_TOKEN
        self.unit = unit_name(self.home)
        # ZERO's HTTP door's systemd units (.socket, .service): systemd holds
        # its port and starts it at the first knock
        self.http_door_unit = unit_name(self.home, HTTP_DOOR_UNIT)

    @property
    def http_door_port(self):
        """The HTTP door's port, read from config.toml when asked."""
        return http_door_port(self.home)

    def environment(self):
        """The variable that leads a server to this home."""
        return {HOME_ENV: str(self.home)}

    def make(self):
        """Create the folders, readable by the user only."""
        for folder in (
            self.home, self.state, self.sessions, self.trajectories,
            self.locks, self.run,
        ):
            folder.mkdir(parents=True, exist_ok=True)
            folder.chmod(0o700)
