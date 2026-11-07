"""Where ZERO keeps its files: one home, ~/.zero ($ZERO_HOME moves it), so
the harness stays portable and deleting the home leaves a clean ZERO.

At the home's top are the user's files: config.toml (the settings,
settings.py), keys.toml (API keys), models.toml (the model servers they
added, model/), SOUL.md (who ZERO is), ZERO.md (the user, their machines,
their instructions and what ZERO remembers; both written from the templates
on first use) and permissions.toml (the permissions they saved from an ask);
all optional. state/ is the host's and lasts: the database, session logs,
trajectories, locks, jobs' output, the trash, host.log and crashes.log. run/
holds the running host's socket and locks. The sandbox hides the whole home
from commands. Each home's host runs as a systemd unit of its own: zero-host
for ~/.zero, zero-host-<hash> for any other, so two homes' hosts run side by
side.

Shared by the host and its clients, so neither imports the other. Read when
called, so a test's ZERO_HOME counts; nothing here names a user or a
machine.
"""

import hashlib
import os
from pathlib import Path

NAME = "zero"
# The variable that moves the home
HOME_ENV = "ZERO_HOME"
# The longest path a Unix socket can bind to (sun_path, less its end)
SOCKET_MAX = 107
SOCKET = "zero.sock"
UNIT = f"{NAME}-host"


def usual_home():
    """The home when nothing moves it."""
    return Path.home() / f".{NAME}"


def home():
    """The home: ZERO_HOME's folder, made whole (a host started elsewhere,
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


def model_features_file():
    """What each model was measured to honour of its API's features
    (model/features.py): ZERO's own record, not the user's to edit."""
    return Paths().state / "model_features.json"


def config_file():
    return home() / "config.toml"


def run_folder(top):
    """The running host's folder: run/ in the home, or, when a socket there
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


def unit_name(top):
    """The systemd unit a home's host runs as."""
    if Path(top).absolute() == usual_home().absolute():
        return UNIT
    tag = hashlib.sha256(str(top).encode()).hexdigest()[:8]
    return f"{UNIT}-{tag}"


class Paths:
    def __init__(self, top=None):
        self.home = Path(top) if top else home()
        self.state = self.home / "state"
        self.db = self.state / "state.sqlite"
        self.sessions = self.state / "sessions"
        self.trajectories = self.state / "trajectories"
        self.locks = self.state / "locks"
        # Each job's output (jobs/), and deleted sessions (host/trash.py)
        self.jobs = self.state / "jobs"
        self.trash = self.state / "trash"
        # A detached host's output, and the CLI's crashes
        self.host_log = self.state / "host.log"
        self.crash_log = self.state / "crashes.log"
        self.run = run_folder(self.home)
        self.socket = self.run / SOCKET
        # Held by the host for its whole life: whoever holds it owns the socket
        self.lock = self.run / "host.lock"
        # Held by a client while it starts a host, so two start one
        self.start_lock = self.run / "start.lock"
        self.unit = unit_name(self.home)

    def environment(self):
        """The variable that leads a host to this home."""
        return {HOME_ENV: str(self.home)}

    def make(self):
        """Create the folders, readable by the user only."""
        for folder in (
            self.home, self.state, self.sessions, self.trajectories,
            self.locks, self.run,
        ):
            folder.mkdir(parents=True, exist_ok=True)
            folder.chmod(0o700)
