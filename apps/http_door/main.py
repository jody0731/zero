"""ZERO's HTTP door's own entry: the command line execs this file for `zero web`
and `zero http-door …`, as it does `zero mcp serve`'s, so the command line
never loads the door and the door never loads the command line.

    zero web                      the web UI in the browser: the door found
                                  or started, the page opened signed in
    zero http-door serve          the door, here (systemd's service runs it;
                                  --idle-seconds N)
    zero http-door install        systemd holds the door's port from login
    zero http-door uninstall      ...and no longer
    zero http-door status         where it is, whether systemd holds it, and
                                  the lines that point Claude Code and Codex
                                  at it
"""

import argparse
import sys
import webbrowser
from pathlib import Path

if __name__ == "__main__":
    # Run as a script: ZERO's folder in place of this one
    folder = str(Path(__file__).resolve().parents[2])
    if sys.flags.safe_path:
        sys.path.insert(0, folder)
    else:
        sys.path[0] = folder

from apps.http_door import launcher, systemd_units  # noqa: E402
from apps.http_door.door import IDLE_SECONDS, Door, handed_socket  # noqa: E402
from client.connection import Connection, NotRunning  # noqa: E402
from client.launcher import ensure_server  # noqa: E402
from paths import Paths  # noqa: E402

# The name an agent's entry gives ZERO
MCP_NAME = "zero"


def mcp_url(paths):
    return f"{launcher.url(paths)}/mcp"


def agent_lines(paths):
    """The lines that point Claude Code and Codex at the door (ZERO writes
    no other agent's settings: the user runs them)."""
    url = mcp_url(paths)
    return [
        f"Claude Code: claude mcp add --transport http --scope user {MCP_NAME} "
        f"{url}",
        f"Codex: codex mcp add {MCP_NAME} --url {url}",
        "(an entry that runs `zero mcp serve` goes first: claude mcp remove "
        f"{MCP_NAME} --scope user, codex mcp remove {MCP_NAME})",
    ]


def serve(paths, idle_seconds):
    listener = handed_socket()
    try:
        door = Door(paths, paths.http_door_port, listener=listener,
                    idle_seconds=idle_seconds)
    except OSError as error:
        print(f"ZERO's HTTP door couldn't listen on 127.0.0.1:"
              f"{paths.http_door_port}: {error}", file=sys.stderr)
        return 1
    how = "handed by systemd" if listener is not None else "its own"
    print(f"ZERO's HTTP door on 127.0.0.1:{paths.http_door_port} ({how}); "
          f"leaves after {idle_seconds:g} s idle.", flush=True)
    door.serve()
    return 0


def web(paths, opener=webbrowser.open):
    """The page, opened signed in."""
    report = (lambda text: print(text, file=sys.stderr))
    try:
        # ZERO's server makes the app token the sign-in is made from
        Connection(paths, ensure=lambda: ensure_server(paths, report=report)
                   ).hello()
        launcher.ensure_door(paths)
        link = launcher.sign_in_url(paths)
    except NotRunning as error:
        print(error, file=sys.stderr)
        return 1
    print(f"ZERO's web UI: {launcher.url(paths)}")
    if not opener(link):
        print("No browser opened here: zero web on a machine with one, or "
              "open the address above and sign in from there.",
              file=sys.stderr)
        return 1
    return 0


def status(paths):
    held = systemd_units.held(paths)
    answering = launcher.knock(paths.http_door_port, 0.5) and (
        launcher.answers(paths))
    how = (f"held by systemd, {paths.http_door_unit}.socket" if held
           else "not held by systemd")
    now = "answering" if answering else "not running"
    print(f"ZERO's HTTP door: {launcher.url(paths)} ({how}; {now})")
    for line in agent_lines(paths):
        print(line)
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(prog="zero http-door")
    parser.add_argument("action", choices=("serve", "install", "uninstall",
                                           "status", "web"))
    parser.add_argument("--idle-seconds", type=float, default=IDLE_SECONDS,
                        help="serve: seconds with nothing to do before it "
                             "leaves")
    args = parser.parse_args(argv)
    paths = Paths()
    if args.action == "serve":
        return serve(paths, args.idle_seconds)
    if args.action == "web":
        return web(paths)
    if args.action == "status":
        return status(paths)
    if args.action == "install":
        ok, line = systemd_units.install(paths)
        print(line, file=sys.stdout if ok else sys.stderr)
        if ok:
            for each in agent_lines(paths):
                print(each)
        return 0 if ok else 1
    ok, line = systemd_units.uninstall(paths)
    print(line)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
