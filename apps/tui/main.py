"""The full screen's own entry: `zero`, `zero new` and `zero -r`. The command
line (apps/cli/main.py) execs this file in its place with the words it was
given, as it does server/main.py and apps/mcp/main.py, so the command line never
loads the full screen, nor the full screen the command line.

Usage:
    zero [new] [-r] [--mode MODE] [--model NAME]
"""

import argparse
import sys
from pathlib import Path

if __name__ == "__main__":
    # Run as a script: ZERO's folder in place of this one, whose names
    # (config, main) would hide ZERO's own
    sys.path[0] = str(Path(__file__).resolve().parents[2])

import settings  # noqa: E402
from apps.tui import config  # noqa: E402
from client import session_list  # noqa: E402
from client.connection import Connection, NotRunning, ProtocolError  # noqa: E402
from client.launcher import Hung, ensure_server  # noqa: E402
from paths import Paths  # noqa: E402

# The server checks a mode's name: the full screen knows no modes of its own
MODES_HELP = "the mode the session starts in (/mode in zero lists them)"


def parser():
    parser = argparse.ArgumentParser(
        prog="zero", usage="zero [new] [-r] [--mode MODE] [--model NAME]",
        description="ZERO's full screen: this folder's latest session, a new "
        "one (new) or the session list (-r).",
    )
    parser.add_argument("new", nargs="?", choices=("new",),
                        help="a new session, this folder its workspace")
    parser.add_argument("-r", "--resume", dest="r", action="store_true",
                        help="open the session list")
    parser.add_argument("--mode", help=MODES_HELP)
    parser.add_argument("--model", help="the model the session starts with")
    return parser


def interactive(connection, args, workspace, older):
    """Which session the full screen opens: at /, the last one the user
    worked in; elsewhere that folder's latest, or a new one made on the first
    message. `zero new` a new one; `-r` the list."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise LookupError("zero needs a terminal; scripts use: zero -p [TEXT]")
    greeting = connection.hello()
    defaults = {name: greeting.get(name)
                for name in ("mode", "model", "modes", "default_model")}
    session, show_list = None, False
    if not args.new:
        live = connection.sessions(archived=False)
        session, show_list = session_list.session_to_open(
            workspace, greeting.get("last_session"), live)
        show_list = show_list or args.r
    # Textual loads only here, once the words are known to be right
    from apps.tui.app import run_full_screen

    return run_full_screen(
        connection, workspace, session=session, mode=args.mode, model=args.model,
        defaults=defaults, show_list=show_list, older=older,
    )


def main(argv=None):
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    paths = Paths()
    # Whether a busy server of older code stays until it is idle, as the
    # launcher said (the status line shows it)
    older_kept = []

    def report(text):
        print(text, file=sys.stderr)

    try:
        # The full screen's own settings under [tui], once
        config.move()
        connection = Connection(paths, ensure=lambda: ensure_server(
            paths, report=report, kept_older=lambda: older_kept.append(True)))
        return interactive(
            connection, args, str(Path.cwd().resolve()),
            older=lambda: bool(older_kept),
        )
    except (NotRunning, ProtocolError, LookupError, Hung,
            settings.SettingsError) as error:
        print(error, file=sys.stderr)
        return 1
    except OSError as error:
        # The socket's folder, say: a Unix socket's path can't pass 107 bytes
        print(f"ZERO: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
