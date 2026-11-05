"""ZERO's command: the terminal client, and the host when asked.

Usage:
    zero                     open this folder's latest session (at /: the last
                             one you worked in), or a new one on your first line
    zero new                 a new session, this folder its workspace
    zero -r                  open the session list
    zero -p TEXT [--json] [--session ID] [--mode MODE] [--model NAME]
                             for scripts: send, wait until the session settles,
                             print the answer (or every event, as JSON lines)
    zero host status         whether the host runs, and what keeps it busy
    zero host stop [--force] stop it (asked first if busy)
    zero host [--model NAME] run the host in this terminal
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

import settings  # noqa: E402
from apps.cli import config, update  # noqa: E402
from client import launcher, render, session_list  # noqa: E402
from client.connection import (  # noqa: E402
    Connection, NoAnswer, NotRunning, ProtocolError,
)
from client.launcher import (  # noqa: E402
    Hung, busy_text, ensure_host, hello, host_pid,
)
from client.render import duration, jobs_text  # noqa: E402
from paths import Paths  # noqa: E402

NAME = "ZERO"


def find(sessions, sid):
    """A session by its id or the start of it; the newest with none given."""
    if sid is None:
        return sessions[0] if sessions else None
    matches = [s for s in sessions if s["id"].startswith(sid)]
    if len(matches) != 1:
        found = "no session" if not matches else f"{len(matches)} sessions"
        raise LookupError(f"{found} matching {sid!r}")
    return matches[0]


def read_session(host, sid=None):
    """A session's row (its open ask included) and its whole log, from the
    host. `host`: a Connection, or anything with sessions() and history()."""
    info = find(host.sessions(), sid)
    if info is None:
        return None, []
    return info, host.history(info["id"], limit=None)[0]


def history_text(info, records, working=True):
    lines = [
        f"{info['id']}  {info['state']}  {info['mode']}  {info['model']}  "
        f"{info['workspace']}",
        info["title"], "",
    ]
    started = None
    restarts = 0
    for record in records:
        clock = datetime.fromisoformat(record["time"]).strftime("%H:%M:%S")
        if record["type"] == "session" and record.get("note"):
            lines.append(f"--- {record['note']} ---")
        elif record["type"] == "note" and record.get("kind") in (
            "restarted", "upgraded"
        ):
            lines.append(f"--- {record['text']} ---")
            restarts += record.get("kind") == "restarted"
        elif record["type"] == "user":
            if not record.get("steer"):
                started, restarts = record["time"], 0
            mark = " (steer)" if record.get("steer") else ""
            # A background job's notice isn't the user's
            who = "job" if record.get("source") == "job" else "you"
            lines.append(f"{clock}  {who}{mark}  {record['text']}")
        elif record["type"] == "ask":
            why = render.reason(record["why"])
            asked = f"asks: {record['command']}" + (f" ({why})" if why else "")
            lines.append(f"{clock}  {NAME}  {asked}")
        elif record["type"] == "answer":
            reply = "approved" if record["yes"] else "replied; not run"
            lines.append(f"{clock}  you  {reply}")
        elif record["type"] == "turn_end":
            lines.append(f"{clock}  {NAME}  {ending(record, started, restarts)}")
    ask = info.get("ask")
    if working and ask is not None:
        age = waited(datetime.now().astimezone() - datetime.fromtimestamp(
            ask["created"]
        ).astimezone())
        lines.append(f"          {NAME}  (waiting {age} for your answer)")
    elif working and info["state"] in ("running", "waiting"):
        lines.append(f"          {NAME}  (working on it)")
    return "\n".join(lines)


def waited(delta):
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 120:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    return f"{minutes // 60} hours"


def show(host, sid=None):
    """A session's questions and answers, from its log, then its jobs."""
    info, records = read_session(host, sid)
    if info is None:
        return "No sessions yet."
    text = history_text(info, records)
    jobs = host.jobs(info["id"])
    if jobs:
        text += "\n\njobs:\n" + jobs_text(jobs)
    return text


def list_sessions(host):
    lines = []
    for info in host.sessions():
        clock = datetime.fromtimestamp(info["updated"]).astimezone()
        lines.append(
            f"{info['id']}  {info['state']}  {info['mode']}  "
            f"{clock:%Y-%m-%d %H:%M}  {info['workspace']}  {info['title']}"
        )
    return "\n".join(lines) if lines else "No sessions yet."


def ending(record, started=None, restarts=0):
    """The answer, or how the turn ended without one."""
    if record["outcome"] == "answered":
        return f"{record['answer']}  ({duration(record, started, restarts)})"
    why = record.get("detail")
    if record.get("error"):
        why = record["error"].strip().splitlines()[-1]
    text = f"({record['outcome']}: {why})" if why else f"({record['outcome']})"
    if started and restarts:
        text += f"  ({duration(record, started, restarts)})"
    return text


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


def host_status(paths):
    try:
        reply = hello(paths)
    except NoAnswer:
        print(f"ZERO's host (pid {host_pid(paths)}) doesn't answer; stop it with: "
              "zero host stop --force")
        return 1
    if reply is None:
        print("ZERO's host isn't running.")
        return 1
    started = datetime.fromtimestamp(reply["started"]).astimezone()
    print(f"ZERO's host is running (pid {reply['pid']}, since {started:%H:%M}, "
          f"build {reply['build']}): {busy_text(reply['busy'])}.")
    return 0


def host_stop(paths, force=False, confirm=None, seconds=20):
    """Stop the host cleanly, asking first while anything runs. `force`: for a
    host that doesn't answer, SIGTERM and then SIGKILL."""
    if force:
        return kill_host(paths)
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


# The host checks a mode's name: this command knows no modes of its own
MODES_HELP = "the mode the session starts in (/mode in zero lists them)"
COMMANDS = ("zero", "zero new", "zero -r", "zero -p TEXT", "zero host status|stop",
            "zero install|uninstall", "zero update")
OWN = ("new", "host", "install", "uninstall", "update")


def parser_for(argv):
    """(parser, args): `args.command` is one of OWN or None (a bare `zero`,
    `-r` or `-p`)."""
    if argv and argv[0] in OWN:
        parser = argparse.ArgumentParser(prog=f"zero {argv[0]}")
        if argv[0] == "new":
            parser.add_argument("--mode", help=MODES_HELP)
            parser.add_argument("--model")
        elif argv[0] == "host":
            parser.add_argument("action", nargs="?", default="run",
                                choices=("run", "status", "stop"))
            parser.add_argument("--model", help="run: new sessions' default model")
            parser.add_argument(
                "--force", action="store_true",
                help="stop: SIGTERM, then SIGKILL, for a host that hangs",
            )
        elif argv[0] == "install":
            parser.add_argument("--bin", metavar="DIR",
                                help="where the launcher goes (~/.local/bin)")
        args = parser.parse_args(argv[1:])
        args.command = argv[0]
        return parser, args
    parser = argparse.ArgumentParser(
        prog="zero", usage="zero [new | -r | -p TEXT] [--mode MODE] [--model NAME]",
        description="ZERO, a local work agent. Commands: " + ", ".join(COMMANDS),
    )
    parser.add_argument("-r", action="store_true", help="open the session list")
    parser.add_argument("-p", metavar="TEXT",
                        help="send TEXT, wait until the session settles, print "
                             "the answer (for scripts)")
    parser.add_argument("--json", action="store_true",
                        help="with -p: print every event as a JSON line")
    parser.add_argument("--session", metavar="ID",
                        help="with -p: this session (its id or the start of "
                             "it), not a new one")
    parser.add_argument("--mode", help=MODES_HELP)
    parser.add_argument("--model")
    parser.add_argument("words", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    args.command = None
    return parser, args


def print_turn(connection, args, workspace, out=print):
    """`zero -p`: send, follow the session until it settles (Pi's
    `agent_settled`: a queued message or a job's notice may start another
    turn after one ends), print the answer. Exits 0 answered, 1 not, 2 when a
    question waits (printed, with how to answer it)."""
    sid, after, reply_to = None, 0, None
    if args.session is not None:
        info = find(connection.sessions(), args.session)
        sid = info["id"]
        records, _ = connection.history(sid, limit=1)
        after = records[-1]["seq"] if records else 0
        # A reply to the waiting question, marked for it, as its box sends one
        if info.get("ask") is not None:
            reply_to = info["ask"]["id"]
    reply = connection.send(args.p, session=sid, mode=args.mode, model=args.model,
                            workspace=workspace, reply_to=reply_to)
    sid = reply["session"]
    last = None
    with connection.watch(sid, after=after) as stream:
        for event in stream:
            if args.json:
                out(json.dumps(event, ensure_ascii=False))
            record = event.get("record", {})
            if record.get("type") == "turn_end":
                last = record
            settled = event.get("settled")
            if settled is None:
                continue
            ask = settled.get("ask")
            if ask is not None:
                if not args.json:
                    out(render.ask_line(ask))
                    out(f"Answer with: zero -p [TEXT] --session {sid}")
                return 2
            if last is None:
                if not args.json:
                    out("(nothing ran)")
                return 1
            if not args.json:
                out(ending(last))
            return 0 if last["outcome"] == "answered" else 1
    raise NotRunning("ZERO's watch ended before the session settled")


def interactive(connection, args, workspace, older):
    """Which session a bare `zero` opens: at /, the last one
    the user worked in; elsewhere that folder's latest, or a new one made on
    the first message. `zero new` a new one; `-r` the list."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise LookupError("zero needs a terminal; scripts use: zero -p [TEXT]")
    greeting = connection.hello()
    defaults = {name: greeting.get(name)
                for name in ("mode", "model", "modes", "default_model")}
    session, show_list = None, False
    if args.command != "new":
        live = connection.sessions(archived=False)
        session, show_list = session_list.opening(workspace, greeting.get("last"), live)
        show_list = show_list or args.r
    from apps.cli.app import terminal

    return terminal(
        connection, workspace, session=session, mode=args.mode, model=args.model,
        defaults=defaults, show_list=show_list, older=older,
    )


def main(argv=None, start_command=None, start_popen=subprocess.Popen, unit=None,
         systemd=None):
    """`start_command`, `start_popen`, `unit` and `systemd` are for tests: the
    host's argv, how a detached one is spawned, a throwaway unit name, and
    whether to use systemd (None: ask it)."""
    parser, args = parser_for(sys.argv[1:] if argv is None else argv)
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
    paths = Paths()
    notes = []
    # The CLI's own settings under [cli], once, before any host reads the file
    config.move()

    def report(text):
        notes.append(text)
        print(text, file=sys.stderr)

    options = {"command": start_command, "popen": start_popen, "systemd": systemd,
               "report": report}
    if unit is not None:
        options["unit"] = unit
    connection = Connection(paths, ensure=lambda: ensure_host(paths, **options))
    workspace = str(Path.cwd().resolve())
    try:
        if args.command == "host":
            if args.action == "status":
                return host_status(paths)
            if args.action == "stop":
                return host_stop(paths, force=args.force)
            run_host(args.model)
        elif args.command is None and args.p is not None:
            return print_turn(connection, args, workspace)
        else:
            return interactive(
                connection, args, workspace,
                older=lambda: any("replaced once it is idle" in n for n in notes),
            )
    except (NotRunning, ProtocolError, LookupError, Hung,
            settings.SettingsError) as error:
        print(error, file=sys.stderr)
        return 1
    except OSError as error:
        # The socket's folder, say: a Unix socket's path can't pass 107 bytes
        print(f"ZERO: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
