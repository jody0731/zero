"""ZERO's plain commands: everything a caller does, a person or another
agent, each sending one of the protocol's messages (protocol/messages.py).
A message only a screen needs has no command; NO_COMMAND says why.

    zero session send TEXT [--session ID] [--cd DIR] [--thinking-effort E]
        [--json | --events]
    zero session list [--archived | --all] [--cd DIR] [--watch]
    zero session read ID · watch ID · stop ID · continue ID
    zero session rename ID TITLE · archive ID · unarchive ID · delete ID
    zero session set ID [--mode M] [--model M] [--cd DIR] [--thinking-effort E]
        [--judge-model M] [--judge-thinking-effort E]
    zero job list [--session ID] · job stop JOB
    zero grant list ID · grant forget ID GRANT
    zero model list · model features MODEL [--test] · model server list ·
        probe NAME · connect NAME --model M [--url URL] [--key-stdin] ·
        disconnect NAME
    zero setting list · setting set NAME VALUE
    zero server hello · server stop (main.py's, with server status and server run)

How a message becomes a command:
- Its name is its message's: a family (a noun), then the action; `zero
  session stop ID` sends `session/stop`. A shortcut sends one of its
  family's messages. `zero -p TEXT` is short for `zero
  session send TEXT`.
- A session, a job or a server comes first, by its id or the start of it.
- The message's other fields are flags by their names; the workspace is
  `--cd`, as /cd is in the full screen.
- A long text is the argument, or `-` to read it from stdin. A key never is
  an argument (shell history and `ps` would show it): `--key-stdin` reads it.
- Output is lines for a person; `--json` prints the server's reply as one JSON
  object, an error included ({"ok": false, "error": ...}). The streams
  (`session watch`, `session list --watch`) print one JSON object a line.
- Exit 0 done, 1 not (the error on stderr), 2 a question waits.

`session send` and `session continue` follow the session until it settles
(Pi's `agent_settled`: a queued message or a job's notice may start another
turn), then print the answer, or with `--json` one result object: session,
outcome, answer, ask, tokens and seconds; `--events` prints every event as it
comes; `--no-wait` only the session's id.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from client import render
from client.connection import NotRunning, ProtocolError
from client.render import duration, jobs_text
from client.result import CHOICES, Result

NAME = "ZERO"
# The families main.py hands here; `server` is main.py's own
FAMILIES = ("session", "job", "grant", "model", "setting")
# The messages with no command, and why: what only a screen needs
NO_COMMAND = {
    "session/mark_seen": "a person's read mark, which the full screen sends for what "
                    "it has shown; a caller reads a session with session read",
}
THINKING_EFFORT_HELP = "the model's thinking effort from the next turn"
MODES_HELP = "the mode (/mode in zero lists them)"


# ------------------------------------------------------------- readers


def match_session(sessions, sid):
    """A session by its id or the start of it; the newest with none given."""
    if sid is None:
        return sessions[0] if sessions else None
    matches = [s for s in sessions if s["id"].startswith(sid)]
    if len(matches) != 1:
        found = "no session" if not matches else f"{len(matches)} sessions"
        raise LookupError(f"{found} matching {sid!r}")
    return matches[0]


def read_session(server, sid=None):
    """A session's row (its open ask included) and its whole log, from the
    server. `server`: a Connection, or anything with sessions() and history()."""
    info = match_session(server.sessions(), sid)
    if info is None:
        return None, []
    return info, server.history(info["id"], limit=None)[0]


def history_text(info, records, working=True, runs_with=None):
    """A session's transcript as text: its row, what its next turn runs
    with (`runs_with`, the server's word, where given) and its records."""
    lines = [
        f"{info['id']}  {info['state']}  {info['mode']}  {info['model']}  "
        f"{info['workspace']}",
        info["title"], "",
    ]
    if runs_with:
        judge = runs_with.get("judge_model") or "the session's model"
        lines.insert(1, f"thinking {runs_with.get('thinking_effort')} · "
                        f"judge {judge}, "
                        f"thinking {runs_with.get('judge_thinking_effort')}")
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
            ask["created_at"]
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


def show(server, sid=None):
    """A session's questions and answers, from its log, then its jobs."""
    info, records = read_session(server, sid)
    if info is None:
        return "No sessions yet."
    text = history_text(info, records)
    jobs = server.jobs(info["id"])
    if jobs:
        text += "\n\njobs:\n" + jobs_text(jobs)
    return text


def list_sessions(server, sessions=None):
    lines = []
    for info in server.sessions() if sessions is None else sessions:
        clock = datetime.fromtimestamp(info["updated_at"]).astimezone()
        mark = "  (a question waits)" if info.get("ask") else ""
        lines.append(
            f"{info['id']}  {info['state']}  {info['mode']}  "
            f"{clock:%Y-%m-%d %H:%M}  {info['workspace']}  {info['title']}{mark}"
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


# ------------------------------------------------------- send and follow


def text_of(value, stdin=None):
    """A long text: the argument, or stdin for `-`."""
    if value == "-":
        return (stdin or sys.stdin).read()
    return value


def follow(connection, sid, after, args, out):
    """Follow a session from `after` until it settles: the answer, the
    result object (`--json`) or every event (`--events`). Exits 0 answered,
    1 not, 2 when a question waits (printed, with how to answer it)."""
    as_json = getattr(args, "json", False)
    events = getattr(args, "events", False)
    result = Result(sid)
    with connection.watch(sid, after=after) as stream:
        for event in stream:
            if events:
                out(json.dumps(event, ensure_ascii=False))
            if not result.add(event):
                continue
            if as_json:
                out(json.dumps(result.as_json(), ensure_ascii=False))
            elif not events:
                if result.ask is not None:
                    out(render.ask_line(result.ask))
                    out(f"Answer with: zero -p [TEXT] --session {sid}, or "
                        f"--choice {'|'.join(CHOICES)}")
                elif result.last is None:
                    out("(nothing ran)")
                else:
                    out(ending(result.last))
            return result.exit_code()
    raise NotRunning("ZERO's watch ended before the session settled")


def print_turn(connection, args, workspace, out=print):
    """`zero session send` (and `zero -p`): send, then follow the session
    until it settles."""
    sid, after, reply_to = None, 0, None
    if args.session is not None:
        info = match_session(connection.sessions(), args.session)
        sid = info["id"]
        records, _ = connection.history(sid, limit=1)
        after = records[-1]["seq"] if records else 0
        # A reply to the waiting question, marked for it, as its box sends one
        if info.get("ask") is not None:
            reply_to = info["ask"]["id"]
    options = {"session": sid, "mode": args.mode, "model": args.model,
               "workspace": workspace, "reply_to": reply_to,
               "thinking_effort": getattr(args, "thinking_effort", None)}
    if getattr(args, "when_busy", None):
        options["when_busy"] = args.when_busy
    if getattr(args, "choice", None):
        options["choice"] = args.choice
    reply = connection.send(text_of(args.p), **options)
    sid = reply["session"]
    if getattr(args, "no_wait", False):
        out(json.dumps(reply) if getattr(args, "json", False) else sid)
        return 0
    return follow(connection, sid, after, args, out)


def continue_session(connection, args, out=print):
    """`zero session continue`: the turn goes on, followed until it settles."""
    sid = match_session(connection.sessions(), args.id)["id"]
    records, _ = connection.history(sid, limit=1)
    after = records[-1]["seq"] if records else 0
    reply = connection.continue_session(sid)
    if getattr(args, "no_wait", False):
        out(json.dumps(reply) if args.json else sid)
        return 0
    return follow(connection, sid, after, args, out)


# --------------------------------------------------------------- parser


def json_flag(parser):
    parser.add_argument("--json", action="store_true",
                        help="print the server's reply as one JSON object")


def follow_flags(parser):
    shown = parser.add_mutually_exclusive_group()
    shown.add_argument("--json", action="store_true",
                       help="when it settles, one JSON object: session, "
                            "outcome, answer, ask, tokens, seconds")
    shown.add_argument("--events", action="store_true",
                       help="every event as it comes, one JSON object a line")
    parser.add_argument("--no-wait", action="store_true",
                        help="print the session's id and return at once")


def send_flags(parser):
    """`session send`'s flags; `zero -p` takes the same."""
    parser.add_argument("--session", metavar="ID",
                        help="this session (its id or the start of it), not "
                             "a new one")
    parser.add_argument("--mode", help=MODES_HELP)
    parser.add_argument("--model")
    parser.add_argument("--thinking-effort", "--effort", dest="thinking_effort",
                        help="a new session's thinking effort: off, low, high "
                             "or max")
    parser.add_argument("--cd", metavar="DIR",
                        help="a new session's workspace (this folder)")
    parser.add_argument("--when-busy", choices=("steer", "queue"),
                        help="while a turn runs: steer it (the default) or "
                             "queue a follow-up")
    parser.add_argument("--choice", choices=CHOICES,
                        help="answer the waiting question with one of its "
                             "choices")
    follow_flags(parser)


def session_arg(parser, help="a session: its id or the start of it"):
    parser.add_argument("id", metavar="ID", help=help)


def parser(prog="zero"):
    """Every family's commands, as argparse subparsers; each action's
    `handler` is its function below."""
    top = argparse.ArgumentParser(
        prog=prog, description="Z.E.R.O.'s plain commands; each mirrors one "
        "of the protocol's messages.",
    )
    families = top.add_subparsers(dest="family", required=True)

    session = families.add_parser("session", help="sessions")
    actions = session.add_subparsers(dest="action", required=True)
    send = actions.add_parser("send", help="a message to a session or a new one")
    send.add_argument("p", metavar="TEXT", help="the message; - reads stdin")
    send_flags(send)
    send.set_defaults(handler=send_command)
    go = actions.add_parser("continue", help="a turn that ended goes on")
    session_arg(go)
    follow_flags(go)
    go.set_defaults(handler=continue_command)
    stop = actions.add_parser("stop", help="stop the running turn")
    session_arg(stop)
    json_flag(stop)
    stop.set_defaults(handler=stop_command)
    listing = actions.add_parser("list", help="the sessions, newest first")
    which = listing.add_mutually_exclusive_group()
    which.add_argument("--archived", action="store_true",
                       help="only the archived ones")
    which.add_argument("--all", action="store_true", help="archived ones too")
    listing.add_argument("--cd", metavar="DIR",
                         help="only those whose workspace is DIR")
    listing.add_argument("--watch", action="store_true",
                         help="the list again at each change, until Ctrl+C")
    json_flag(listing)
    listing.set_defaults(handler=list_command)
    read = actions.add_parser("read", help="a session's log")
    session_arg(read)
    read.add_argument("--after", type=int, default=0, metavar="SEQ")
    read.add_argument("--limit", type=int, metavar="N",
                      help="only the newest N records")
    read.add_argument("--before", type=int, metavar="SEQ")
    json_flag(read)
    read.set_defaults(handler=read_command)
    watch = actions.add_parser(
        "watch", help="a session's records, then each new one, until Ctrl+C",
    )
    session_arg(watch)
    watch.add_argument("--after", type=int, default=0, metavar="SEQ")
    watch.add_argument("--until-settled", action="store_true",
                       help="end when nothing more happens on its own")
    watch.add_argument("--json", action="store_true",
                       help="each event as one JSON object a line")
    watch.set_defaults(handler=watch_command)
    rename = actions.add_parser("rename", help="give a session a title")
    session_arg(rename)
    rename.add_argument("title", metavar="TITLE")
    json_flag(rename)
    rename.set_defaults(handler=rename_command)
    for name, about in (("archive", "out of the list, kept"),
                        ("unarchive", "back to the list"),
                        ("delete", "its files to the trash")):
        action = actions.add_parser(name, help=about)
        session_arg(action)
        json_flag(action)
        action.set_defaults(handler=session_edit_command)
    change = actions.add_parser("set", help="mode, model, workspace, thinking "
                                            "effort or judge, from the next turn")
    session_arg(change)
    change.add_argument("--mode", help=MODES_HELP)
    change.add_argument("--model")
    change.add_argument("--cd", metavar="DIR", help="the workspace")
    change.add_argument("--thinking-effort", "--effort", dest="thinking_effort",
                        help=THINKING_EFFORT_HELP)
    change.add_argument("--judge-model", metavar="M",
                        help='the judge\'s model ("": the session\'s own)')
    change.add_argument("--judge-thinking-effort", "--judge-effort",
                        dest="judge_thinking_effort", metavar="E",
                        help="the judge's thinking effort: off, low, high or max")
    json_flag(change)
    change.set_defaults(handler=set_command)

    job = families.add_parser("job", help="background jobs")
    actions = job.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list", help="every running job, or a "
                                              "session's, ended ones too")
    listing.add_argument("--session", metavar="ID")
    json_flag(listing)
    listing.set_defaults(handler=job_list_command)
    stop = actions.add_parser("stop", help="stop a job")
    stop.add_argument("job", metavar="JOB", help="a job's id or the start of it")
    json_flag(stop)
    stop.set_defaults(handler=job_stop_command)

    grant = families.add_parser("grant", help="what an ask's \"don't ask "
                                              "again\" allowed")
    actions = grant.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list", help="the grants that hold in a session")
    session_arg(listing)
    json_flag(listing)
    listing.set_defaults(handler=grant_list_command)
    forget = actions.add_parser("forget", help="take a grant back")
    session_arg(forget)
    forget.add_argument("grant", metavar="GRANT", help="its id (grant list "
                                                       "--json)")
    json_flag(forget)
    forget.set_defaults(handler=grant_forget_command)

    model = families.add_parser("model", help="models and their servers")
    actions = model.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list", help="every reachable server's models")
    json_flag(listing)
    listing.set_defaults(handler=model_list_command)
    measured = actions.add_parser(
        "features", help="what a model honours of its API's features")
    measured.add_argument("model", metavar="MODEL")
    measured.add_argument("--test", action="store_true",
                          help="measure again now (a few calls to the model)")
    json_flag(measured)
    measured.set_defaults(handler=model_features_command)
    server = actions.add_parser("server", help="model servers and keys")
    verbs = server.add_subparsers(dest="verb", required=True)
    listing = verbs.add_parser("list", help="every server ZERO knows")
    json_flag(listing)
    listing.set_defaults(handler=server_list_command)
    probe = verbs.add_parser("probe", help="a server's models")
    probe.add_argument("name", metavar="NAME", nargs="?",
                       help="a server's name (server list)")
    probe.add_argument("--url", help="a server of one's own, its /v1 url")
    probe.add_argument("--key-stdin", action="store_true",
                       help="read the key from stdin")
    json_flag(probe)
    probe.set_defaults(handler=probe_command)
    connect = verbs.add_parser("connect", help="a model tested, then saved")
    connect.add_argument("name", metavar="NAME",
                         help="the server's name: what its models start with")
    connect.add_argument("--model", required=True, help="the server's model id")
    connect.add_argument("--url", help="a server of one's own, its /v1 url")
    connect.add_argument("--key-stdin", action="store_true",
                         help="read the key from stdin; none read for a "
                              "server that needs none")
    connect.add_argument("--context-window", "--context", type=int,
                         metavar="TOKENS",
                         help="its context window, when the server doesn't say")
    connect.add_argument("--title")
    connect.add_argument("--default", action="store_true",
                         help="new sessions' model from now on")
    json_flag(connect)
    connect.set_defaults(handler=connect_command)
    disconnect = verbs.add_parser("disconnect", help="a server's key and "
                                                     "lines taken out")
    disconnect.add_argument("name", metavar="NAME")
    json_flag(disconnect)
    disconnect.set_defaults(handler=disconnect_command)

    setting = families.add_parser("setting", help="ZERO's settings")
    actions = setting.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list", help="every setting and its value")
    json_flag(listing)
    listing.set_defaults(handler=setting_list_command)
    put = actions.add_parser("set", help="write one to config.toml")
    put.add_argument("name", metavar="NAME")
    put.add_argument("value", metavar="VALUE",
                     help="JSON (80, true, null) or else a word")
    json_flag(put)
    put.set_defaults(handler=setting_set_command)
    return top


def run(argv, connection, out=print, stdin=None):
    """One command: its exit status. Errors to stderr; with --json also as
    the server's error object on stdout."""
    args = parser().parse_args(argv)
    args.stdin = stdin or sys.stdin
    try:
        return args.handler(connection, args, out)
    except (ProtocolError, LookupError, ValueError) as error:
        if getattr(args, "json", False):
            out(json.dumps({"ok": False, "error": str(error)}))
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


# ------------------------------------------------------------- handlers


def print_reply(args, out, reply, text):
    out(json.dumps(reply, ensure_ascii=False) if args.json else text)
    return 0


def session_id(connection, prefix):
    return match_session(connection.sessions(), prefix)["id"]


def workspace_of(folder):
    return str(Path(folder or ".").resolve())


def send_command(connection, args, out):
    return print_turn(connection, args, workspace_of(args.cd), out)


def continue_command(connection, args, out):
    return continue_session(connection, args, out)


def stop_command(connection, args, out):
    reply = connection.stop(session_id(connection, args.id))
    return print_reply(args, out, reply,
                       "Stopped." if reply.get("stopped") else "Nothing was running.")


def list_command(connection, args, out):
    archived = True if args.archived else None if args.all else False
    workspace = workspace_of(args.cd) if args.cd else None
    if args.watch:
        with connection.watch_list(archived, workspace) as stream:
            for event in stream:
                if args.json:
                    out(json.dumps(event, ensure_ascii=False))
                elif "sessions" in event:
                    out(list_sessions(connection, event["sessions"]) + "\n")
                if "bye" in event:
                    return 0
        return 0
    sessions = connection.sessions(archived=archived, workspace=workspace)
    return print_reply(args, out, {"ok": True, "sessions": sessions},
                       list_sessions(connection, sessions))


def read_command(connection, args, out):
    info = match_session(connection.sessions(), args.id)
    reply = connection.read(info["id"], after=args.after, limit=args.limit,
                            before=args.before)
    text = history_text(info, reply["records"], runs_with=reply.get("runs_with"))
    return print_reply(args, out, reply, text)


def watch_command(connection, args, out):
    sid = session_id(connection, args.id)
    shown = render.Renderer(source=connection.source)
    with connection.watch(sid, after=args.after) as stream:
        for event in stream:
            if args.json:
                out(json.dumps(event, ensure_ascii=False))
            else:
                for line in shown.event(event):
                    out(line)
            if "bye" in event or (args.until_settled and "settled" in event):
                return 0
    return 0


def rename_command(connection, args, out):
    reply = connection.rename(session_id(connection, args.id), args.title)
    return print_reply(args, out, reply, "Renamed.")


def session_edit_command(connection, args, out):
    sid = session_id(connection, args.id)
    if args.action == "delete":
        trash = connection.delete(sid)
        return print_reply(args, out, {"ok": True, "trash": trash},
                           f"Deleted; its files are in {trash}.")
    reply = connection.archive(sid, archived=args.action == "archive")
    return print_reply(args, out, reply, f"{args.action.capitalize()}d.")


def set_command(connection, args, out):
    given = {"mode": args.mode, "model": args.model,
             "thinking_effort": args.thinking_effort,
             "workspace": workspace_of(args.cd) if args.cd else None,
             "judge_model": args.judge_model,
             "judge_thinking_effort": args.judge_thinking_effort}
    if not any(value is not None for value in given.values()):
        raise ValueError("session set takes --mode, --model, --cd, --thinking-effort, "
                         "--judge-model or --judge-thinking-effort")
    reply = connection.set(session_id(connection, args.id), **given)
    # A judge model of None is the session's own again
    own = "the session's own"
    said = ", ".join(f"{k} {own if v is None else v}"
                     for k, v in reply.get("set", {}).items())
    when = " from its next turn" if reply.get("next_turn") else ""
    return print_reply(args, out, reply, f"Set {said}{when}.")


def job_list_command(connection, args, out):
    sid = session_id(connection, args.session) if args.session else None
    jobs = connection.jobs(sid)
    text = jobs_text(jobs, sessions=sid is None) or "No jobs."
    return print_reply(args, out, {"ok": True, "jobs": jobs}, text)


def job_stop_command(connection, args, out):
    running = [row["id"] for row in connection.jobs()
               if row["id"].startswith(args.job)]
    job = running[0] if len(running) == 1 else args.job
    reply = connection.stop_job(job)
    return print_reply(args, out, reply,
                       "Stopped." if reply["stopped"] else "It wasn't running.")


def grant_list_command(connection, args, out):
    grants = connection.grants(session_id(connection, args.id))
    lines = [f"{g['id']}  {g['description']}  (by {g['by']}, {g['scope']})"
             for g in grants]
    return print_reply(args, out, {"ok": True, "grants": grants},
                       "\n".join(lines) or "No grants.")


def grant_forget_command(connection, args, out):
    reply = connection.forget_grant(session_id(connection, args.id), args.grant)
    return print_reply(args, out, reply, f"Forgot {args.grant}.")


def model_list_command(connection, args, out):
    reply = connection.models()
    lines = []
    for server in reply["servers"]:
        if server.get("error"):
            lines.append(f"{server['server']}: {server['error']}")
        else:
            lines += server["models"] or [f"{server['server']}: no models"]
    return print_reply(args, out, reply, "\n".join(lines) or "No servers reachable.")


def model_features_command(connection, args, out):
    reply = connection.features(args.model, test=args.test)
    words = {True: "yes", False: "unavailable", None: "not measured"}
    lines = [f"{reply['model']} on {reply['api']}, measured "
             f"{reply['measured_at']}:"]
    lines += [f"  {name}: {words[honoured]}"
              for name, honoured in reply["features"].items()]
    return print_reply(args, out, reply, "\n".join(lines))


def server_list_command(connection, args, out):
    reply = connection.servers()
    lines = [f"{row['name']}  key {row['key_state']}  {row['url']}"
             for row in reply["servers"]]
    lines.append(f"default model: {reply['default'] or 'none'}")
    if reply.get("not_ready_reason"):
        lines.append(f"(a new session couldn't start: {reply['not_ready_reason']})")
    return print_reply(args, out, reply, "\n".join(lines))


def key_of(args):
    """The key from stdin, when asked for; else None (the stored one)."""
    if not args.key_stdin:
        return None
    return args.stdin.read().strip()


def probe_command(connection, args, out):
    if args.name is None and args.url is None:
        raise ValueError("model server probe takes a NAME or --url")
    url, models = connection.probe(name=args.name, url=args.url,
                                   key=key_of(args))
    lines = [url] + [
        f"  {m['id']}" + (f"  ({m['context_window']} tokens)"
                          if m.get("context_window")
                          else "")
        for m in models
    ]
    return print_reply(args, out, {"ok": True, "url": url, "models": models},
                       "\n".join(lines))


def connect_command(connection, args, out):
    reply = connection.connect(
        args.name, args.model, url=args.url, key=key_of(args),
        context_window=args.context_window, title=args.title,
        default=True if args.default else None,
    )
    said = f"Connected {reply['model']}: its test call answered."
    if reply.get("default"):
        said += " New sessions use it."
    return print_reply(args, out, reply, said)


def disconnect_command(connection, args, out):
    reply = connection.disconnect(args.name)
    said = f"Removed {reply['removed']}."
    if reply.get("default_unset"):
        said += " New sessions have no model until one is connected."
    return print_reply(args, out, reply, said)


def setting_list_command(connection, args, out):
    settings, error = connection.settings()
    lines = [f"{row['name']} = {json.dumps(row['value'])}  ({row['label']})"
             for row in settings]
    if error:
        lines.append(f"config.toml: {error}")
    return print_reply(args, out, {"ok": True, "settings": settings, "error": error},
                       "\n".join(lines))


def setting_set_command(connection, args, out):
    try:
        value = json.loads(args.value)
    except json.JSONDecodeError:
        value = args.value
    reply = connection.set_setting(args.name, value)
    shown = ", ".join(f"{k} = {json.dumps(v)}" for k, v in reply["set"].items())
    return print_reply(args, out, reply, f"Set {shown}.")
