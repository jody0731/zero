"""ZERO's command line: the plain commands, and the words that start the full
screen, `zero mcp serve` and the server, each an entry of its own.

Usage:
    zero                     open this folder's latest session (at /: the last
                             one you worked in), or a new one on your first line
    zero new                 a new session, this folder its workspace
    zero -r                  open the session list
    zero -p TEXT [--json | --events] [--session ID] [--cd DIR] ...
                             zero session send: send, wait until the session
                             settles, print the answer (--json: one result
                             object; --events: every event, as JSON lines)
    zero session|job|grant|model|setting|mcp ...
                             the plain commands, one for each of the
                             protocol's messages (commands.py)
    zero server status         whether the server runs, and what keeps it busy
                             (zero server hello the same; --json its reply)
    zero server start [--json] start it if none runs, and print the way in
                             for an app in any language (its socket, token
                             file, protocol, release)
    zero server stop [--force] stop it (asked first if busy)
    zero server [--model NAME] run the server in this terminal
    zero mcp serve           ZERO as an MCP server on stdio, for other agents
    zero mcp list|get|add|remove
                             the MCP servers whose tools ZERO uses
    zero install [--bin DIR] [--dir DIR]
                             from a release: put it in the install folder
                             (~/.local/share/zero) and the zero command on the
                             PATH (~/.local/bin); from a clone, the command
    zero uninstall           remove the command, and an install's releases
    zero update              an installed ZERO to its newest release
    zero --version           this copy of ZERO's version

Full screen. Inside, Enter sends (a steer while a turn runs), Shift+Enter
starts a new line, Ctrl+J or Alt+Enter queues a follow-up, Tab (or Ctrl+O) goes
to the sessions and back, Shift+Tab steps the mode (/mode, /model and /cd
change a session from its next turn), Alt+Down goes to the next session that
waits for you or has news, PgUp and PgDn scroll, Ctrl+C stops the turn and
gives back what it hadn't taken, Ctrl+D leaves; / lists the slash commands,
/help the keys. An ask is a box in place of the input: answer it in your own
words.

Every turn runs in the server, and every command reaches it through its socket.
A command starts the server when none answers: a transient systemd user unit,
zero-server, or a detached process where there is no systemd. Nothing starts at
login, and the server leaves after its idle minutes with no turn, job, queued
message or client. A session's workspace is a folder, / for the whole machine;
its commands run there, read-only in the sandbox until file tools come.
Background jobs outlive their turn and the terminal; their session is told.

Modes: each a sandbox and who answers asks, least to most permissive, as the
server names them (/mode in zero lists them with a line each; Shift+Tab steps
through all but the one where nothing asks). A command may ask to run
outside the sandbox, and a turn past its step limit asks to go on. A plan (propose_plan)
goes to the same approver; what an ask's "don't ask again" grants is listed by
/permissions. --model chooses the model; without it a new session takes
config.toml's (your default model).

Files: ~/.zero/ ($ZERO_HOME moves it) holds everything ZERO keeps. At its top:
SOUL.md and ZERO.md (who ZERO is; the user's instructions, written on first
use), config.toml (what /settings sets: the theme, new sessions' mode and
models, the limits, the idle time, the keys), keys.toml (API keys),
models.toml (the model servers you added) and permissions.toml (what you saved
from an ask). ~/.zero/state/ holds state.sqlite, sessions/, locks/, jobs/ (each
job's output) and server.log (a detached server's output). ~/.zero/run/ holds the
socket (zero.sock) and the locks (server.lock, start.lock); app-token, at the
home's top, proves the user where the socket can't tell. Install writes
~/.local/bin/zero. A release (its code, packages and Python) sits in
~/.local/share/zero/versions/, and current names the one that runs; an update
adds one and switches current, never touching the home. A git clone runs on
its own .venv, and an installed one moves to the releases on its first start.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

if __name__ == "__main__":
    # Run as a script (the zero on the PATH): ZERO's folder in place of this
    # one, whose names (config, main) would hide ZERO's own
    folder = str(Path(__file__).resolve().parents[2])
    # With -I (ZERO's own Pythons), which leaves this one out, before
    # the rest
    if sys.flags.safe_path:
        sys.path.insert(0, folder)
    else:
        sys.path[0] = folder

from apps.cli import commands, move_from_clone, update  # noqa: E402
from apps.cli.install_folder import (  # noqa: E402
    Busy, InstallError, InstallFolder, default_bin, is_release_folder_in,
    release_file, remove, running_from, try_release,
)
# The readers live with the commands; tests and the evals reach them here
from apps.cli.commands import (  # noqa: E402, F401
    ending, match_session, history_text, list_sessions, print_turn, read_session, show,
)
from client import launcher  # noqa: E402
from client.connection import (  # noqa: E402
    Connection, NoAnswer, NotRunning, ProtocolError, command_session,
)
from client.launcher import (  # noqa: E402
    Hung, busy_text, ensure_server, hello, server_pid,
)
from paths import (  # noqa: E402
    LAUNCHER_READ_BYTES, Paths, changelog_version, own_python, release_command,
    release_folder, write_whole,
)

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


def path_note(launcher):
    if str(launcher.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"{launcher.parent} isn't on PATH; add it to run zero by name.")


def install(folder=None, install_dir=None):
    """`zero install`. From a release (an archive unpacked): the release put
    in the install folder (`install_dir`, else ~/.local/share/zero) and
    switched to, and the zero command in `folder` (else ~/.local/bin) run
    it; then `zero doctor`. From a clone or a tree: a launcher that runs
    this copy on this Python. Nothing starts at login. Exit code 0 when
    installed."""
    release = release_folder(ROOT)
    if release is not None:
        return install_release(release, folder, install_dir)
    script = Path(__file__).resolve()
    python = Path(sys.executable).absolute()
    launcher = Path(folder) / "zero" if folder else launcher_path()
    write_whole(
        launcher,
        f'#!/bin/sh\nexec {shell_path(python)} {shell_path(script)} "$@"\n',
    )
    launcher.chmod(0o755)
    print(f"Installed {launcher}.")
    path_note(launcher)
    return 0


def install_release(release, folder=None, install_dir=None, execv=os.execv):
    """A release folder into the install folder. One outside it is copied in
    and placed, and its copy finishes (an archive unpacked anywhere, as by
    install.sh); one inside is tried, switched to and given the command."""
    target = InstallFolder(Path(install_dir).absolute() if install_dir else None)
    bin_folder = Path(folder).absolute() if folder else default_bin()
    version = release_file(release).get("version")
    if not version:
        print(f"{release} has no version in its release.json.", file=sys.stderr)
        return 1
    try:
        if not is_release_folder_in(release, target):
            with target.locked():
                target.sweep()
                if target.current_version() != version:
                    target.place(target.copy_in(release), version)
            placed = release_command(target.release(version))
            argv = [str(placed), "install", "--bin", str(bin_folder),
                    "--dir", str(target.path)]
            execv(argv[0], argv)
            return 0
        with target.locked():
            problem = try_release(release, version)
            if problem:
                raise InstallError(f"ZERO {version} didn't run here: {problem}")
            target.tidy({version, target.current_version()})
            target.switch(version)
            target.sweep()
            command = target.write_command(bin_folder)
    except (Busy, InstallError, OSError) as error:
        print(f"ZERO wasn't installed: {error}", file=sys.stderr)
        return 1
    print(f"Installed ZERO {version} in {target.path}; its command is {command}.")
    path_note(command)
    # Said before the doctor takes this process's place
    sys.stdout.flush()
    run_doctor()
    return 0


def uninstall():
    """`zero uninstall`: the zero command removed, and an install folder's
    releases with it when nothing else runs from them; the home stays."""
    release = release_folder(ROOT)
    home = Paths().home
    if release is None:
        launcher = launcher_path()
        launcher.unlink(missing_ok=True)
        print(f"Removed {launcher}. ZERO's home stays in {home}.")
        return 0
    target = InstallFolder(release.parent.parent)
    users = running_from(target.path)
    if users is None or users:
        print(f"Something runs from {target.path} "
              f"({', '.join(map(str, users or [])) or 'no /proc to look in'}); "
              "stop ZERO's server first (zero server stop).", file=sys.stderr)
        return 1
    removed = []
    for launcher in {launcher_path(), *filter(None, [shutil.which("zero")])}:
        launcher = Path(launcher)
        try:
            with open(launcher, errors="replace") as stream:
                ours = str(target.path) in stream.read(LAUNCHER_READ_BYTES)
        except OSError:
            ours = False
        if ours:
            launcher.unlink()
            removed.append(str(launcher))
    remove(target.path)
    print(f"Removed {', '.join([*removed, str(target.path)])}. ZERO's home "
          f"stays in {home}.")
    return 0


def server_status(paths, as_json=False):
    """`zero server hello` (and `status`): whether the server runs, never
    starting one; `as_json`, its hello as one object."""
    try:
        reply = hello(paths)
    except NoAnswer:
        reply = None
        said = (f"ZERO's server (pid {server_pid(paths)}) doesn't answer; stop it "
                "with: zero server stop --force")
    else:
        said = "ZERO's server isn't running."
    if reply is None:
        print(json.dumps({"ok": False, "error": said}) if as_json else said)
        return 1
    if as_json:
        print(json.dumps(reply, ensure_ascii=False))
        return 0
    started = datetime.fromtimestamp(reply["started_at"]).astimezone()
    print(f"ZERO's server is running (pid {reply['pid']}, since {started:%H:%M}, "
          f"build {reply['build']}): {busy_text(reply['busy_with'])}.")
    return 0


def server_start(paths, connection, as_json=False):
    """`zero server start`: the server started when none answers, as any
    command starts it, then the way in for an app in any language: its
    socket, the token file where the socket can't tell who connects, the
    protocol and the release (`as_json`: one object)."""
    reply = connection.hello()
    way_in = {
        "ok": True, "socket": str(paths.socket),
        "token_file": str(paths.app_token),
        "protocol": reply["protocol"], "version": reply.get("version"),
        "pid": reply["pid"],
    }
    if as_json:
        print(json.dumps(way_in, ensure_ascii=False))
        return 0
    print(f"ZERO's server is running (pid {reply['pid']}, release "
          f"{reply.get('version') or '?'}, protocol {reply['protocol']}).\n"
          f"Its socket: {paths.socket}\nIts token file: {paths.app_token}")
    return 0


def server_stop(paths, force=False, confirm=None, seconds=20, as_json=False):
    """Stop the server cleanly, asking first while anything runs. `force`: for a
    server that doesn't answer, SIGTERM and then SIGKILL. `as_json`: one
    server/stop, its reply printed, nothing asked."""
    if force:
        return kill_server(paths)
    if as_json:
        try:
            reply = launcher.shutdown(paths)
        except NoAnswer:
            reply = {"ok": False, "error": (
                f"ZERO's server (pid {server_pid(paths)}) doesn't answer; stop "
                "it with: zero server stop --force")}
        except NotRunning:
            reply = {"ok": False, "error": "ZERO's server isn't running."}
        print(json.dumps(reply, ensure_ascii=False))
        return 0 if reply.get("stopping") else 1
    try:
        reply = launcher.shutdown(paths)
    except NoAnswer:
        print(f"ZERO's server (pid {server_pid(paths)}) doesn't answer; stop it with: "
              "zero server stop --force")
        return 1
    except NotRunning:
        print("ZERO's server isn't running.")
        return 0
    if not reply.get("ok"):
        raise ProtocolError(reply.get("error", "shutdown refused"))
    cut = not reply["stopping"]
    if cut:
        confirm = confirm or ask_yes
        if not confirm(f"{busy_text(reply['busy_with'])}: stop them? [y/N] "):
            print("Left running.")
            return 1
        launcher.shutdown(paths, force=True)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if hello(paths, timeout=0.5) is None:
                print("Stopped ZERO's server." + (
                    " What it cut off resumes at the next start." if cut else ""
                ))
                return 0
        except NoAnswer:
            pass
        time.sleep(0.1)
    print("ZERO's server is still stopping; if it hangs: zero server stop --force")
    return 1


def ask_yes(question):
    if not sys.stdin.isatty():
        return False
    try:
        return input(question).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def kill_server(paths, grace=10):
    """SIGTERM, then SIGKILL after `grace` seconds (OpenCode's)."""
    pid = server_pid(paths)
    if not pid.isdigit():
        print("ZERO's server isn't running.")
        return 0
    pid = int(pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print("ZERO's server isn't running.")
        return 0
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print(f"Stopped ZERO's server (pid {pid}).")
            return 0
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    print(f"Killed ZERO's server (pid {pid}); what it cut off resumes at the next "
          "start.")
    return 0


def run_server(model=None):
    """The server in this terminal: server/main.py in this process's place, so
    this command never loads the server's code."""
    argv = [*own_python(), str(launcher.SERVER_SCRIPT)]
    argv += ["--model", model] if model else []
    os.execv(sys.executable, argv)


# The other apps' own entries, each run in this process's place: the
# command line imports none of them, nor they it
TUI_SCRIPT = ROOT / "apps" / "tui" / "main.py"
MCP_SCRIPT = ROOT / "apps" / "mcp" / "main.py"
# `zero doctor`'s, which loads the harness to try what ZERO needs
DOCTOR_SCRIPT = ROOT / "doctor.py"


def run_tui(argv):
    """`zero`, `zero new`, `zero -r`: the full screen, given the words as
    they came."""
    os.execv(sys.executable, [*own_python(), str(TUI_SCRIPT), *argv])


def run_mcp():
    """`zero mcp serve`: ZERO as an MCP server."""
    os.execv(sys.executable, [*own_python(), str(MCP_SCRIPT)])


def run_doctor():
    """`zero doctor`: what ZERO needs from the machine, tried."""
    os.execv(sys.executable, [*own_python(), str(DOCTOR_SCRIPT)])


COMMANDS = ("zero", "zero new", "zero -r", "zero -p TEXT", "zero session …",
            "zero job …", "zero grant …", "zero model …", "zero setting …",
            "zero server hello|stop|status|run", "zero mcp serve|list|get|add|remove",
            "zero install|uninstall", "zero update", "zero doctor")
OWN_COMMANDS = ("server", "mcp", "install", "uninstall", "update", "doctor")
# The full screen's word; its flags are its own (apps/tui/main.py)
TUI_COMMANDS = ("new",)


def parser_for(argv):
    """(parser, args): `args.command` is one of OWN_COMMANDS, "family" (commands.py's
    families), "tui" (`zero new`) or None (a bare `zero`, `-r` or `-p`)."""
    if argv and argv[0] in TUI_COMMANDS:
        parser = argparse.ArgumentParser(prog="zero")
        args = argparse.Namespace(command="tui", p=None, words=[])
        return parser, args
    # `zero mcp serve` is ZERO as an MCP server, its own entry; the rest of
    # `zero mcp` is the family of the MCP servers ZERO uses
    if argv and argv[0] in commands.FAMILIES and argv[:2] != ["mcp", "serve"]:
        parser = commands.parser()
        args = parser.parse_args(argv)
        args.command = "family"
        return parser, args
    if argv and argv[0] in OWN_COMMANDS:
        parser = argparse.ArgumentParser(prog=f"zero {argv[0]}")
        if argv[0] == "server":
            parser.add_argument("action", nargs="?", default="run",
                                choices=("run", "hello", "status", "start",
                                         "stop"))
            parser.add_argument("--model", help="run: new sessions' default model")
            parser.add_argument(
                "--force", action="store_true",
                help="stop: SIGTERM, then SIGKILL, for a server that hangs",
            )
            parser.add_argument("--json", action="store_true",
                                help="hello, status, start, stop: the reply "
                                     "as one JSON object")
        elif argv[0] == "mcp":
            # list, get, add and remove are the family's (commands.py)
            parser.add_argument("action", choices=("serve",),
                                help="serve: ZERO as an MCP server on stdio")
        elif argv[0] == "install":
            parser.add_argument("--bin", metavar="DIR",
                                help="where the zero command goes (~/.local/bin)")
            parser.add_argument("--dir", metavar="DIR",
                                help="a release's install folder "
                                     "(~/.local/share/zero)")
        args = parser.parse_args(argv[1:])
        args.command = argv[0]
        return parser, args
    parser = argparse.ArgumentParser(
        prog="zero", usage="zero [new | -r | -p TEXT] [--mode MODE] [--model NAME]",
        description="Z.E.R.O., a general purpose agent. Commands: "
        + ", ".join(COMMANDS),
    )
    parser.add_argument("-r", "--resume", dest="r", action="store_true",
                        help="open the session list")
    parser.add_argument("-p", "--print", dest="p", metavar="TEXT",
                        help="zero session send TEXT: send, wait until the "
                             "session settles, print the answer (for scripts); "
                             "- reads stdin")
    commands.send_flags(parser)
    parser.add_argument("--version", action="version",
                        version=f"zero {changelog_version() or '(no CHANGELOG.md)'}",
                        help="this copy of ZERO's version")
    parser.add_argument("words", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.command = None
    return parser, args


def argv_of(argv):
    return sys.argv[1:] if argv is None else argv


def moves_first(args):
    """Whether this command moves an installed clone first: the full screen,
    -p and the plain commands, outside a session's commands."""
    return args.command in (None, "tui", "family") and command_session() is None


def main(argv=None, start_command=None, start_popen=subprocess.Popen, unit=None,
         systemd=None):
    """`start_command`, `start_popen`, `unit` and `systemd` are for tests: the
    server's argv, how a detached one is spawned, a throwaway unit name, and
    whether to use systemd (None: ask it)."""
    parser, args = parser_for(argv_of(argv))
    # `new` after the flags (`zero --mode auto new`) is the full screen's too
    if args.command is None and args.p is None and args.words == ["new"]:
        args.command, args.words = "tui", []
    if args.command is None and args.words:
        # A stray word would start a session: it is refused
        print(f"zero: unknown command {' '.join(args.words)!r}. Commands: "
              + ", ".join(COMMANDS) + ". To send a message: zero -p [TEXT]",
              file=sys.stderr)
        return 2
    # An install from a git clone moves to its own Python first (7.5.3): not
    # in a session's command, a diagnosis, nor what installs or updates
    if argv is None and moves_first(args):
        move_from_clone.run(ROOT, sys.argv[1:])
    # These change the code or the command, never the home
    if args.command == "update":
        return update.update(ROOT)
    if args.command == "install":
        return install(args.bin, args.dir)
    if args.command == "uninstall":
        return uninstall()
    if args.command == "mcp":
        return run_mcp()
    if args.command == "doctor":
        return run_doctor()
    if args.command == "tui" or (args.command is None and args.p is None):
        return run_tui(argv_of(argv))
    paths = Paths()
    options = {"command": start_command, "popen": start_popen, "systemd": systemd,
               "report": lambda text: print(text, file=sys.stderr)}
    if unit is not None:
        options["unit"] = unit
    connection = Connection(paths, ensure=lambda: ensure_server(paths, **options))
    try:
        if args.command == "server":
            if args.action in ("hello", "status"):
                return server_status(paths, args.json)
            if args.action == "start":
                return server_start(paths, connection, args.json)
            if args.action == "stop":
                return server_stop(paths, force=args.force, as_json=args.json)
            run_server(args.model)
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
