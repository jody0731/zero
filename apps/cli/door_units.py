"""systemd's units for ZERO's HTTP door, kept as an install changes: written
by `zero install`, by a clone's move to its first release and by `zero
update`, removed by `zero uninstall` (apps/http_door/systemd_units.py does
it, run as the door's own entry, which the command line never imports).

Only for the usual home (~/.zero): a test's home, a copy of the user's (the
home-copy check) or a second home never touch the user's systemd; a home of
its own takes its door with `zero http-door install`.
"""

import subprocess
import sys
from pathlib import Path

from paths import Paths, own_python, usual_home

# The door's entry, beside this app's
HTTP_DOOR_SCRIPT = Path(__file__).resolve().parents[1] / "http_door" / "main.py"
UNITS_SECONDS = 60


def for_usual_home():
    return Paths().home.absolute() == usual_home().absolute()


def keep(action, command=None, run=subprocess.run):
    """`zero http-door install|uninstall`, as `command` (a release's zero)
    runs it, else this copy; its lines printed. Whether it ran."""
    if not for_usual_home():
        return False
    argv = ([str(command), "http-door", action] if command is not None
            else [*own_python(), str(HTTP_DOOR_SCRIPT), action])
    try:
        run(argv, timeout=UNITS_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"ZERO's HTTP door's units: {error}", file=sys.stderr)
    return True
