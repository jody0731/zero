"""ZERO's command line: the plain commands, and the words that start the full
screen, `zero mcp serve` and the host, each an entry of its own.

Usage:
    zero                     open this folder's latest session (at /: the last
                             one you worked in), or a new one on your first line
    zero new                 a new session, this folder its workspace
    zero -r                  open the session list
    zero -p TEXT [--json | --events] [--session ID] [--cd DIR] ...
                             zero session send: send, wait until the session
                             settles, print the answer (--json: one result
                             object; --events: every event, as JSON lines)
    zero session|job|grant|model|setting ...
                             the plain commands, one for each of the
                             protocol's messages (commands.py)
    zero host status         whether the host runs, and what keeps it busy
                             (zero host hello the same; --json its reply)
    zero host stop [--force] stop it (asked first if busy)
    zero host [--model NAME] run the host in this terminal
    zero mcp serve           ZERO as an MCP server on stdio, for other agents
    zero install [--bin DIR] put the zero command on the PATH (~/.local/bin)
    zero uninstall           remove the command
    zero update              an installed ZERO to its newest release

Full screen. Inside, Enter sends (a steer while a turn runs), Shift+Enter
starts a new line, Ctrl+J or Alt+Enter queues a follow-up, Tab (or Ctrl+O) goes
to the sessions and back, Shift+Tab steps the mode (/mode, /model and /cd
change a session from its next turn), Alt+Down goes to the next session that
waits for you or has news, PgUp and PgDn scroll, Ctrl+C stops the turn and
gives back what it hadn't taken, Ctrl+D leaves; / lists the slash commands,
/help the keys. An ask is a box in place of the input: answer it in your own
words.

Every turn runs in the host, and every command reaches it through its socket.
A command starts the host when none answers: a transient systemd user unit,
zero-host, or a detached process where there is no systemd. Nothing starts at
login, and the host leaves after its idle minutes with no turn, job, queued
message or client. A session's workspace is a folder, / for the whole machine;
its commands run there, read-only in the sandbox until file tools come.
Background jobs outlive their turn and the terminal; their session is told.

Modes, least to most permissive: readOnly (nothing writable, asks refused),
default (you answer asks), auto (a judge answers), auto+ (no sandbox; the
judge reviews every call that needs permission), bypassPermissions (no sandbox,
nothing asked). In default and auto a command may ask to run outside the
sandbox, and a turn past its step limit asks to go on. A plan (propose_plan)
goes to the same approver; what an ask's "don't ask again" grants is listed by
/permissions. --model chooses the model; without it a new session takes
config.toml's (your default model).

Files: ~/.zero/ ($ZERO_HOME moves it) holds everything ZERO keeps. At its top:
SOUL.md and ZERO.md (who ZERO is; the user's instructions, written on first
use), config.toml (what /settings sets: the theme, new sessions' mode and
models, the limits, the idle time, the keys), keys.toml (API keys),
models.toml (the model servers you added) and permissions.toml (what you saved
from an ask). ~/.zero/state/ holds state.sqlite, sessions/, locks/, jobs/ (each
job's output) and host.log (a detached host's output). ~/.zero/run/ holds the
socket (zero.sock) and the locks (host.lock, start.lock). Install writes
~/.local/bin/zero. ZERO's code is wherever it was cloned (install.sh makes its
.venv there); an update changes the code, never the home.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

if __name__ == "__main__":
    # Run as a script (the zero on the PATH): ZERO's folder in place of this
    # one, whose names (config, main) would hide ZERO's own
    sys.path[0] = str(Path(__file__).resolve().parents[2])

from apps.cli import commands, update  # noqa: E402
# The readers live with the commands; tests and the evals reach them here
from apps.cli.commands import (  # noqa: E402, F401
    ending, find, history_text, list_sessions, print_turn, read_session, show,
)
from client import launcher  # noqa: E402
from client.connection import (  # noqa: E402
    Connection, NoAnswer, NotRunning, ProtocolError,
)
from client.launcher import (  # noqa: E402
    Hung, busy_text, ensure_host, hello, host_pid,
)
from paths import Paths  # noqa: E402

NAME = "ZERO"


# ZERO's folder: the clone an update moves
ROOT = Path(__file__).resolve().parents[2]


def launcher_path():
    return Path.home() / ".local" / "bin" / "zero"


def shell_path(path):
    text = str(path)
    for char in ("\\", '"', "$", "`"):
        text = text.replace(char, "\\" + char)
    return '"' + text + '"'


def install(folder=None):
    """Write the launcher: `zero` on the PATH (or in `folder`). Nothing
    starts at login."""
    script = Path(__file__).resolve()
    python = Path(sys.executable).absolute()
    launcher = Path(folder) / "zero" if folder else launcher_path()
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        f'#!/bin/sh\nexec {shell_path(python)} {shell_path(script)} "$@"\n'
    )
    launcher.chmod(0o755)
    print(f"Installed {launcher}.")
    if str(launcher.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"{launcher.parent} isn't on PATH; add it to run zero by name.")


def uninstall():
    launcher = launcher_path()
    launcher.unlink(missing_ok=True)
    print(f"Removed {launcher}. ZERO's home stays in {Paths().home}.")


def host_status(paths, as_json=False):
    """`zero host hello` (and `status`): whether the host runs, never
    starting one; `as_json`, its hello as one object."""
    try:
        reply = hello(paths)
    except NoAnswer:
        reply = None
        said = (f"ZERO's host (pid {host_pid(paths)}) doesn't answer; stop it "
                "with: zero host stop --force")
    else:
        said = "ZERO's host isn't running."
    if reply is None:
        print(json.dumps({"ok": False, "error": said}) if as_json else said)
        return 1
    if as_json:
        print(json.dumps(reply, ensure_ascii=False))
        return 0
    started = datetime.fromtimestamp(reply["started"]).astimezone()
    print(f"ZERO's host is running (pid {reply['pid']}, since {started:%H:%M}, "
          f"build {reply['build']}): {busy_text(reply['busy'])}.")
    return 0


def host_stop(paths, force=False, confirm=None, seconds=20, as_json=False):
    """Stop the host cleanly, asking first while anything runs. `force`: for a
    host that doesn't answer, SIGTERM and then SIGKILL. `as_json`: one
    host/stop, its reply printed, nothing asked."""
    if force:
        return kill_host(paths)
    if as_json:
        try:
            reply = launcher.shutdown(paths)
        except NotRunning:
            reply = {"ok": False, "error": "ZERO's host isn't running."}
        print(json.dumps(reply, ensure_ascii=False))
        return 0 if reply.get("stopping") else 1
    try:
        reply = launcher.shutdown(paths)
    except NoAnswer:
        print(f"ZERO's host (pid {host_pid(paths)}) doesn't answer; stop it with: "
              "zero host stop --force")
        return 1
    except NotRunning:
        print("ZERO's host isn't running.")
        return 0
    if not reply.get("ok"):
        raise ProtocolError(reply.get("error", "shutdown refused"))
    cut = not reply["stopping"]
    if cut:
        confirm = confirm or ask_yes
        if not confirm(f"{busy_text(reply['busy'])}: stop them? [y/N] "):
            print("Left running.")
            return 1
        launcher.shutdown(paths, force=True)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if hello(paths, timeout=0.5) is None:
                print("Stopped ZERO's host." + (
                    " What it cut off resumes at the next start." if cut else ""
                ))
                return 0
        except NoAnswer:
            pass
        time.sleep(0.1)
    print("ZERO's host is still stopping; if it hangs: zero host stop --force")
    return 1


def ask_yes(question):
    if not sys.stdin.isatty():
        return False
    try:
        return input(question).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def kill_host(paths, grace=10):
    """SIGTERM, then SIGKILL after `grace` seconds (OpenCode's)."""
    pid = host_pid(paths)
    if not pid.isdigit():
        print("ZERO's host isn't running.")
        return 0
    pid = int(pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print("ZERO's host isn't running.")
        return 0
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print(f"Stopped ZERO's host (pid {pid}).")
            return 0
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    print(f"Killed ZERO's host (pid {pid}); what it cut off resumes at the next "
          "start.")
    return 0


def run_host(model=None):
    """The host in this terminal: host/main.py in this process's place, so
    this command never loads the host's code."""
    argv = [sys.executable, str(launcher.SCRIPT)]
    argv += ["--model", model] if model else []
    os.execv(sys.executable, argv)


# The other apps' own entries, each run in this process's place: the
# command line imports none of them, nor they it
TUI_SCRIPT = ROOT / "apps" / "tui" / "main.py"
MCP_SCRIPT = ROOT / "apps" / "mcp" / "main.py"


def run_tui(argv):
    """`zero`, `zero new`, `zero -r`: the full screen, given the words as
    they came."""
    os.execv(sys.executable, [sys.executable, str(TUI_SCRIPT), *argv])


def run_mcp():
    """`zero mcp serve`: ZERO as an MCP server."""
    os.execv(sys.executable, [sys.executable, str(MCP_SCRIPT)])


COMMANDS = ("zero", "zero new", "zero -r", "zero -p TEXT", "zero session …",
            "zero job …", "zero grant …", "zero model …", "zero setting …",
            "zero host hello|stop|status|run", "zero mcp serve",
            "zero install|uninstall", "zero update")
OWN = ("host", "mcp", "install", "uninstall", "update")
# The full screen's word; its flags are its own (apps/tui/main.py)
TUI_WORDS = ("new",)


def parser_for(argv):
    """(parser, args): `args.command` is one of OWN, "family" (commands.py's
    families), "tui" (`zero new`) or None (a bare `zero`, `-r` or `-p`)."""
    if argv and argv[0] in TUI_WORDS:
        parser = argparse.ArgumentParser(prog="zero")
        args = argparse.Namespace(command="tui", p=None, words=[])
        return parser, args
    if argv and argv[0] in commands.FAMILIES:
        parser = commands.parser()
        args = parser.parse_args(argv)
        args.command = "family"
        return parser, args
    if argv and argv[0] in OWN:
        parser = argparse.ArgumentParser(prog=f"zero {argv[0]}")
        if argv[0] == "host":
            parser.add_argument("action", nargs="?", default="run",
                                choices=("run", "hello", "status", "stop"))
            parser.add_argument("--model", help="run: new sessions' default model")
            parser.add_argument(
                "--force", action="store_true",
                help="stop: SIGTERM, then SIGKILL, for a host that hangs",
            )
            parser.add_argument("--json", action="store_true",
                                help="hello, status, stop: the reply as one "
                                     "JSON object")
        elif argv[0] == "mcp":
            # The server side alone for now; list, get, add and remove (the
            # servers ZERO itself uses) come with the toolbox
            parser.add_argument("action", choices=("serve",),
                                help="serve: ZERO as an MCP server on stdio")
        elif argv[0] == "install":
            parser.add_argument("--bin", metavar="DIR",
                                help="where the launcher goes (~/.local/bin)")
        args = parser.parse_args(argv[1:])
        args.command = argv[0]
        return parser, args
    parser = argparse.ArgumentParser(
        prog="zero", usage="zero [new | -r | -p TEXT] [--mode MODE] [--model NAME]",
        description="Z.E.R.O., a general purpose agent. Commands: "
        + ", ".join(COMMANDS),
    )
    parser.add_argument("-r", action="store_true", help="open the session list")
    parser.add_argument("-p", metavar="TEXT",
                        help="zero session send TEXT: send, wait until the "
                             "session settles, print the answer (for scripts); "
                             "- reads stdin")
    commands.send_flags(parser)
    parser.add_argument("words", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.command = None
    return parser, args


def argv_of(argv):
    return sys.argv[1:] if argv is None else argv


def main(argv=None, start_command=None, start_popen=subprocess.Popen, unit=None,
         systemd=None):
    """`start_command`, `start_popen`, `unit` and `systemd` are for tests: the
    host's argv, how a detached one is spawned, a throwaway unit name, and
    whether to use systemd (None: ask it)."""
    parser, args = parser_for(argv_of(argv))
    if args.command is None and args.words:
        # A stray word would start a session: it is refused
        print(f"zero: unknown command {' '.join(args.words)!r}. Commands: "
              + ", ".join(COMMANDS) + ". To send a message: zero -p [TEXT]",
              file=sys.stderr)
        return 2
    # These change the code or the command, never the home
    if args.command == "update":
        return update.update(ROOT)
    if args.command == "install":
        install(args.bin)
        return 0
    if args.command == "uninstall":
        uninstall()
        return 0
    if args.command == "mcp":
        return run_mcp()
    if args.command == "tui" or (args.command is None and args.p is None):
        return run_tui(argv_of(argv))
    paths = Paths()
    options = {"command": start_command, "popen": start_popen, "systemd": systemd,
               "report": lambda text: print(text, file=sys.stderr)}
    if unit is not None:
        options["unit"] = unit
    connection = Connection(paths, ensure=lambda: ensure_host(paths, **options))
    try:
        if args.command == "host":
            if args.action in ("hello", "status"):
                return host_status(paths, args.json)
            if args.action == "stop":
                return host_stop(paths, force=args.force, as_json=args.json)
            run_host(args.model)
        elif args.command == "family":
            return commands.run(argv_of(argv), connection)
        else:
            return print_turn(connection, args, commands.workspace_of(args.cd))
    except (NotRunning, ProtocolError, LookupError, Hung) as error:
        print(error, file=sys.stderr)
        return 1
    except OSError as error:
        # The socket's folder, say: a Unix socket's path can't pass 107 bytes
        print(f"ZERO: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
