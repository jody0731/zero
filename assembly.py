"""The assembly: where ZERO is put together.

Each component declares its own settings, tools, notices and hooks; this
collects them (design rule 3: importing a component registers nothing):
every component's SETTINGS and the server kernel's, in the settings view's
order (`SETTINGS_ORDER`); the tools each tool module declares (its TOOLS),
added in the order the model reads them; who sends notices (each sender's
NOTICE_SENDERS); the hooks a session loads in its mode, and their order
(`session_hooks`); a session and its tool context (`make_session`); and what
server/main.py hands the server kernel: how it makes a session
(`server_session_maker`, `server_session`) and the components' services
(`services`).

A mode (permissions/modes.py) picks the sandbox and who decides an ask. The
judge never asks the user. With nobody there to ask (evals, tests: ask=None)
the user's asks are refused. Work nobody watches raises the approver, never
the sandbox: a subagent ZERO started runs default as auto
(`unattended`), and in default a round's asks go to the judge
(DefaultApproval); readOnly is never raised. The evals' server (dev/evalkit/)
hands each session its own box, the shipped SOUL.md and no saved
permissions, and keeps its own clock. No limit stops a turn's spend: ZERO's
estimate is its yardstick (usage/, principle 3). Wherever anything asks, an
11th running job asks too. Who ZERO and the user are comes from SOUL.md and
ZERO.md (prompt/), never from here.
`session_hooks` is a function, not a list, because hooks keep state per session.
Order in the list matters for system-prompt text, and for a note one hook
sets before another reads it (the permissions memory's `granted`, read by
the repeat guard); it never changes how decisions combine (core/hooks.py).
"""

import contextlib
import hashlib
import io
import json
import shutil
from pathlib import Path

import settings
from settings import LiveSettings
import jobs.tool
import shell.tool
import files.tool
import memory.tool
import permissions.tool
import web.tool
import skills.tool
import context.tool
import rounds.tool
import subagents.tool
import wait.tool
import usage.tool
import session_search.tool
import found_tools.tool
from core import history, tools
from core.asking import PARK
from core.loop import Session
from server import SETTINGS as SERVER_SETTINGS
from model import SETTINGS as MODEL_SETTINGS
from model.service import ModelService
from model.servers import Model, ModelServerError, resolve
from model import servers as model_servers
from model.retry import ModelRetry
from limits import SETTINGS as LIMIT_SETTINGS
from context import SETTINGS as CONTEXT_SETTINGS
from context import room
from context.hooks import Context
from context.results import folder_of
from context.size_note import SizeNote
from context.service import ContextService
from limits.repeats import RepeatGuard
from rounds.hooks import Rounds, running_round
from rounds.service import RoundsService
from subagents import SETTINGS as SUBAGENT_SETTINGS
from subagents.hooks import PastEstimate
from subagents.service import SubagentsService
from usage.spend import SessionUse, token_weights
from session_search import SETTINGS as SESSION_SEARCH_SETTINGS
from session_search.service import SessionSearchService
from jobs import SETTINGS as JOB_SETTINGS
from jobs.service import JobsService
from jobs.hooks import JobLimit, RunningJobs
from shell import SETTINGS as SHELL_SETTINGS
from files import SETTINGS as FILE_SETTINGS
from files.seen import ChangedFiles, SeenFiles
from web import SETTINGS as WEB_SETTINGS
from web.tool import web_clients
from judge import SETTINGS as JUDGE_SETTINGS
from judge.hooks import JudgeApproval
from judge.verdict import Judge
from permissions import SETTINGS as PERMISSION_SETTINGS
from permissions.approval import (
    READ_ONLY, DefaultApproval, UserApproval, PermissionAsks, Refuse, ask_user,
)
from permissions.asks import Asks, AskWhen
from permissions.consent import Consent
from permissions.grants import GrantCheck
from permissions.service import PermissionsService
from permissions.modes import (
    JUDGED, MODES, UNSANDBOXED, answers_line, canonical, raised,
)
from limits.stops import BrokenReplyLimit
from prompt import SETTINGS as PROMPT_SETTINGS
from prompt.hooks import Environment, Means
from prompt.instructions import PROJECT_FILE, read, system_prompt, user_instructions
from memory.hooks import ZeroFilesGuard
from skills.found import SkillFolders
from skills.hooks import Skills
from mcp_servers import SETTINGS as MCP_SERVER_SETTINGS
from mcp_servers import listed as mcp_servers_listed
from mcp_servers.hooks import McpServersStarting
from mcp_servers.service import McpServersService
from paths import mcp_servers_file, permissions_file, skills_folder
from permissions.grants import read_saved
from sandbox.check import check_sandbox
from sessions.log import Log, fresh_log
from sandbox.sandbox import (
    Outside, Sandbox, Scratch, Unsandboxed, private_folders, scratch_prefix,
)
from sandbox.service import SandboxService



# The settings view's order: each service declares its own settings (SETTINGS),
# and the view lists them as here
SETTINGS_ORDER = (
    "mode", "model", "fallback_model", "thinking_effort", "time_or_tokens",
    "judge_model", "judge_thinking_effort",
    # The limits, by what they bound: work started, commands, files, the
    # web, the context, session search, notes, the model, the judge, the
    # consent check
    "max_jobs", "max_subagent_depth",
    "bash_foreground_seconds", "bash_output_kept_chars", "repeat_after_seconds",
    "max_file_megabytes",
    "web_timeout", "web_fetch_max_megabytes", "web_fetch_max_redirects",
    "web_search_max_results", "mcp_start_seconds", "mcp_call_seconds",
    "compact_at_tokens", "result_share_percent", "reply_room_tokens",
    "handoff_room_tokens", "context_note_every_tokens",
    "session_search_count", "session_search_each_side",
    "session_search_snippet_words", "rewind_note_line_chars",
    "model_silence_seconds", "model_retries", "model_retry_seconds",
    "max_broken_replies",
    "judge_silence_seconds", "judge_reply_tokens", "judge_retries",
    "judge_output_chars", "consent_silence_seconds",
    "server_idle_minutes",
)
settings.add(
    *SERVER_SETTINGS,
    *MODEL_SETTINGS, *LIMIT_SETTINGS, *CONTEXT_SETTINGS,
    *JOB_SETTINGS, *SUBAGENT_SETTINGS,
    *SHELL_SETTINGS,
    *FILE_SETTINGS, *WEB_SETTINGS, *JUDGE_SETTINGS, *PERMISSION_SETTINGS,
    *SESSION_SEARCH_SETTINGS, *PROMPT_SETTINGS, *MCP_SERVER_SETTINGS,
    order=SETTINGS_ORDER,
)
# The tools, in the order the model reads them (the system message lists
# them so)
tools.add(
    *jobs.tool.TOOLS, *shell.tool.TOOLS, *files.tool.TOOLS,
    *memory.tool.TOOLS, *permissions.tool.TOOLS, *web.tool.TOOLS,
    *skills.tool.TOOLS, *context.tool.TOOLS, *rounds.tool.TOOLS,
    *subagents.tool.TOOLS, *wait.tool.TOOLS, *usage.tool.TOOLS,
    *session_search.tool.TOOLS, *found_tools.tool.TOOLS,
)
# Who sends notices, as the system message names them
for senders in (jobs.tool.NOTICE_SENDERS, rounds.tool.NOTICE_SENDERS,
                subagents.tool.NOTICE_SENDERS):
    history.add_notice_senders(senders)

# Every limit at its default, from the components' settings, their one home
# (the server kernel's own join when it is imported); a session reads its
# limits as they stand (LiveSettings), never a copy
DEFAULT_LIMITS = settings.default_limits()
HERE = Path(__file__).resolve().parent
# The files whose code writes the system message: a change in one, or in the
# tools' schemas, is new code that changes what the model reads (code_mark)
SYSTEM_MESSAGE_SOURCES = (
    "prompt/instructions.py", "prompt/hooks.py", "permissions/modes.py",
    "sandbox/sandbox.py", "memory/hooks.py", "skills/hooks.py",
)


def code_mark(session):
    """What the model reads of the code, as a short hash: the session's
    tools as it tells them, and the source of what writes its system
    message. A new one rebuilds the system message when a turn opens."""
    digest = hashlib.sha256(
        json.dumps(session.tool_schemas(), sort_keys=True).encode()
    )
    for name in SYSTEM_MESSAGE_SOURCES:
        digest.update((HERE / name).read_bytes())
    # Who sends notices, which the system message names
    digest.update(json.dumps(history.NOTICES, sort_keys=True).encode())
    # What each means is for, which the system message lists
    digest.update(json.dumps(tools.means_lines(session.tool_context)).encode())
    # The user's trade between waiting and weighted tokens, stated there too
    limits = session.tool_context.get("limits") or settings.DEFAULTS
    digest.update(limits["time_or_tokens"].encode())
    return digest.hexdigest()[:12]


def session_hooks(
    limits,
    server,
    sandbox,
    mode="default",
    ask=ask_user,
    ask_when=None,
    judge=None,
    outside=None,
    saved=None,
    own=None,
    results=None,
    runs_as=None,
    skills=None,
):
    """`outside`: where a call let out of the sandbox runs; None if it can't.
    `saved`: the user's saved permissions (permissions/grants.py), None for
    none. `own`: what the session knows of ZERO's own files (memory/), None
    where the memory tool isn't offered. `results()`: the session's results
    folder (context/results.py). `runs_as`: the mode whose approver answers,
    where nobody watches the session (a subagent ZERO started: permissions'
    `raised`); None for `mode` itself. `judge`: the judge, which in default
    answers only a round's asks. `limits`: the session's, read at each use
    (settings.LiveSettings). `skills`: the skills folders the session finds
    skills through (skills/found.py's SkillFolders), whose skills its system
    message lists after ZERO's own files; None for none."""
    mode = canonical(mode)
    runs_as = canonical(runs_as or mode)
    spec = MODES[runs_as]
    hooks = [Means(limits, token_weights(server.price)),
             Environment(sandbox, answers_line(mode, runs_as))]
    if own is not None:
        hooks.append(own)
    if skills is not None:
        # The list's room: a share of the session's line, in characters as
        # ZERO's own files count theirs
        def line_chars():
            return room.line(limits, server.context_window) * room.BYTES_PER_TOKEN

        hooks.append(Skills(skills, line_chars))
    hooks += [
        ModelRetry(limits),
        # A model call waits a little for MCP servers still starting
        McpServersStarting(),
        # After Context: a turn's second compaction is told of rounds
        Context(server.context_window, results, limits),
        Rounds(),
        # A subagent's use against the estimate it was started with
        PastEstimate(),
        # After Context: the size a compaction left is what gets told
        SizeNote(limits),
    ]
    # The permissions memory where someone answers asks; before the
    # repeat guard, which lets a granted call through
    if spec.decides in ("user", "judge", "every call"):
        hooks.append(GrantCheck(saved, judged=runs_as in JUDGED))
    hooks += [
        RepeatGuard(limits),
        BrokenReplyLimit(limits),
    ]
    if spec.asks:
        hooks.append(Asks())
    if ask_when is not None and spec.decides is not None:
        hooks.append(AskWhen(ask_when))
    # The jobs' soft cap asks wherever anything asks
    if spec.decides is not None:
        hooks.append(JobLimit())
    if spec.decides == "nobody":
        hooks.append(Refuse(READ_ONLY))
    elif spec.decides == "user":
        # The user answers, or nobody there refuses; in a round, nobody
        # watches, and the judge answers instead
        user = UserApproval(ask) if ask is not None else Refuse()
        hooks.append(DefaultApproval(
            user, JudgeApproval(judge),
            lambda turn: running_round(turn) is not None,
        ))
    elif spec.decides in ("judge", "every call"):
        # auto+ has no rules: what needs permission asks
        if spec.decides == "every call":
            hooks.append(PermissionAsks())
        hooks.append(JudgeApproval(judge))
    # bypassPermissions asks nothing but what a tool says asks in every mode
    # (a change to ZERO's own files)
    else:
        hooks.append(Asks(every_mode=True))
        hooks.append(UserApproval(ask) if ask is not None else Refuse())
    return hooks


class LazyJudge:
    """A judge made at its first use: default's, which answers only in a
    round, so a session that never works in rounds never makes one."""

    def __init__(self, make):
        self._make = make
        self._judge = None

    def __getattr__(self, name):
        if self._judge is None:
            self._judge = self._make()
        return getattr(self._judge, name)


def make_session(
    model,
    folder=None,
    client=None,
    mode="default",
    ask=ask_user,
    limits=None,
    workspace=None,
    ask_when=None,
    saved_permissions=None,
    instructions=None,
    judge_model=None,
    judge_client=None,
    log=None,
    workspace_writes=True,
    thinking_effort=None,
    web=None,
    judge_thinking_effort=None,
    box=None,
    scratch=None,
    fallback_model=None,
    fallback_client=None,
    unattended=False,
    skills=None,
):
    """A session on the chat profile. `limits`: its limits, read at each use:
    a settings.LiveSettings (the server's, config.toml as it stands), or a
    dict of values over the defaults (tests, the evals' scripts; old names
    read as they are now).

    `log`: the session's log (the server's); else a new one named in `folder`
    (tests, the evals' scripts).
    `ask`: how to ask the user; None when nobody is there (evals).
    `ask_when(call)`: True for a call that asks first, in a mode that asks
    (tests).
    `saved_permissions`: the user's file of saved permissions (the server
    passes paths.permissions_file()); None for none, as in the evals.
    `judge_*`: the auto modes' judge; `judge_model` None is the agent's own
    model, whatever it is switched to.
    `instructions`: what leads the system prompt (prompt/instructions.py:
    SOUL.md, the user's ZERO.md, the project's) and whether the memory tool
    may change it; None for nothing (tests).
    `workspace_writes`: False keeps the workspace read-only in every sandboxed
    mode (tests); a session's workspace is writable in default and auto,
    except a workspace of /, which the sandbox keeps read-only.
    `thinking_effort`: the agent's thinking (settings.py), sent with its own
    calls only; None sends nothing.
    `judge_thinking_effort`: the judge's thinking, sent with its calls only;
    None sends nothing, so the API's default holds.
    `web`: what the web tools reach the network with (web/tool.py); None
    builds the session's own (web_clients).
    `box(workspace, private=)`: the sandbox every command runs in, made in
    place of sandbox/'s, whatever the mode: a mode with no sandbox of its
    own keeps it too (the judge isn't told), and no call leaves it. The evals
    hand in theirs (dev/evalkit/: the exam room, the throwaway machine), tests a
    plain Sandbox; None for the mode's own.
    `scratch`: the session's scratch folder (sandbox's Scratch) for the mode's
    own sandbox, which the server keeps across the session's builds; None for
    one of the sandbox's own.
    `fallback_model`: what a turn goes on with when the session's model
    fails past its retries, and what a judge whose model fails asks; None:
    the settings' as they stand at the failure (`limits`), none where they
    name none. `fallback_client`: its client (tests).
    `unattended`: nobody watches the session (a subagent ZERO started), so
    it runs as its mode raised: default as auto, the sandbox kept.
    `skills`: the skills folders it finds skills through (skills/found.py's
    SkillFolders: listed in its system message, read by read_skill, ZERO's
    own folder shown to its commands, read-only); None for none, as in the
    evals and tests, which the user's skills never move.
    """
    if workspace is None:
        raise ValueError("a session needs a workspace")
    mode = canonical(mode)
    spec = MODES[mode]
    runs_as = raised(mode) if unattended else mode
    if not isinstance(limits, LiveSettings):
        limits = LiveSettings(fixed=limits)
    server, _ = resolve(model)
    if log is None and folder is None:
        raise ValueError("a session needs its log, or a folder to start one in")
    session_log = log if log is not None else fresh_log(folder, model)

    def results():
        # A new log is named when its first turn starts
        return folder_of(session_log.path)
    # A fork's copied context names the outputs its parents saved: their
    # folders are shown too, read-only, at their own paths
    head = session_log.first_record() or {}
    shown = [results, *(
        folder_of(session_log.path.parent / f"{ancestor}.jsonl")
        for ancestor in (head.get("forked_from") or {}).get("ancestors", [])
    )]
    # ZERO's own skills sit in its home, which every sandbox hides: their
    # folder is shown again, read-only, so their scripts run as any command
    own_skills = skills.own if skills is not None else None
    if own_skills is not None:
        shown.append(own_skills)
    # A sandbox of its own per session: its scratch folder starts empty, and a
    # session made later can't swap it out
    if box is not None:
        if spec.sandbox == "read-only" or not workspace_writes:
            raise ValueError("a box of its own has no read-only workspace")
        sandbox = box(workspace, private=private_folders())
        sandbox.shown.extend(shown)
        sandbox.own_skills = own_skills
    elif spec.sandbox is not None:
        read_only = spec.sandbox == "read-only" or not workspace_writes
        # A project's ZERO.md, read by later sessions, stays read-only
        # inside, as the workspace's .git does
        protected = (PROJECT_FILE,) if instructions and instructions.project else ()
        sandbox = Sandbox(workspace, read_only=read_only,
                          private=private_folders(), protected=protected,
                          scratch=scratch, shown=shown, own_skills=own_skills)
    else:
        sandbox = Unsandboxed(workspace)
    # Where one call runs once its ask to leave the sandbox is approved: only
    # in a mode whose own sandbox has the workspace, never from a box handed
    # in (an eval's also hides the answer key)
    outside = None
    if spec.sandbox == "workspace" and box is None:
        outside = Outside(workspace, sandbox=sandbox)
    # What leads the system prompt; where the memory tool may write, and what
    # the session knows of those files

    def most_chars():
        # ZERO's own files share what a result ZERO didn't size may take of
        # a fresh context, under the limits as they stand
        at = room.line(limits, server.context_window)
        return room.share(at, limits) * room.BYTES_PER_TOKEN

    found = (read(instructions, sandbox.workspace, most_chars())
             if instructions else None)
    own_files = own = None
    if instructions is not None and instructions.memory:
        own_files = {
            "soul": instructions.soul, "user": instructions.user,
            "project": found.project_file,
        }
        own = ZeroFilesGuard(own_files, found.others, sandbox.workspace)
    def fallback_name():
        # The model a failing call goes on with, as the settings stand now
        return fallback_model or limits["fallback_model"]

    judge = None
    if runs_as in JUDGED or runs_as == "default":
        # Its view of a long session takes half its model's window at most:
        # the rest is its prompt's, the proposed call's and its answer's
        def make_judge(name, client, fallback=None):
            judge_server, _ = resolve(name)
            return Judge(
                name, client, limits=limits, effort=judge_thinking_effort,
                view_bytes=judge_server.context_window // 2 * room.BYTES_PER_TOKEN,
                fallback=fallback,
            )

        def spare_judge():
            # Asked when the judge's model fails to answer at all: one on the
            # fallback model as the settings stand then; None for none
            name = fallback_name()
            if not name or name == (judge_model or model):
                return None
            try:
                return make_judge(name, fallback_client)
            except ModelServerError:
                return None

        def the_judge():
            return make_judge(judge_model or model, judge_client, spare_judge)

        # default's answers only in a round: made when one first asks
        judge = the_judge() if runs_as in JUDGED else LazyJudge(the_judge)
    # The session's record: what it runs with that its log keeps (its
    # limits aren't among them: they are read as they stand)
    settings = {
        "server": server.name,
        "context_limit": server.context_window,
        # USD per 1M tokens at peak (hit, miss, output); evals estimate cost
        "price": server.price,
        "mode": mode,
        "sandbox": sandbox.sandboxed,
        "workspace": str(sandbox.workspace),
        "instructions": found.record() if found is not None else None,
        "out_of_view": [str(p) for p in getattr(sandbox, "covered_folders", list)()],
        # The judge as the user left it, whatever the mode; and what it runs
        # with in this one (None: no judge in this mode)
        "judge_model": judge_model,
        "judge_thinking_effort": judge_thinking_effort,
        "judge": (
            {"model": judge.model, "effort": judge.effort}
            if runs_as in JUDGED else None
        ),
        "kept_sandbox": box is not None and mode in UNSANDBOXED,
        "can_leave_sandbox": outside is not None,
        "thinking_effort": thinking_effort,
    }
    if runs_as != mode:
        # The mode whose approver answers, nobody watching
        settings["runs_as"] = runs_as
    hooks = session_hooks(
        limits, server, sandbox, mode, ask, ask_when, judge, outside,
        saved_permissions, own, results, runs_as, skills,
    )
    # On a resume, the files the cut-off turn touched that changed meanwhile
    hooks.append(ChangedFiles())
    # On an interruption, the jobs the turn leaves running
    hooks.append(RunningJobs())
    def silence():
        # The model's silence as the limits stand at each call
        return limits["model_silence_seconds"]

    llm = Model(model, client, timeout=silence, effort=thinking_effort)
    # Fallback models made at a failure, by name
    spares = {}

    def fallback():
        # What a turn goes on with when the session's model fails past its
        # retries, as the settings stand then; None for none (or one whose
        # server is gone)
        name = fallback_name()
        if not name or name == model:
            return None
        if name not in spares:
            try:
                spares[name] = Model(name, fallback_client, timeout=silence,
                                     effort=thinking_effort)
            except ModelServerError:
                return None
        return spares[name]

    weights = token_weights(server.price)
    prompt = system_prompt(found, model, weights)

    def rebuild():
        # The user's files and the project's, read again as they stand now
        now = (read(instructions, sandbox.workspace, most_chars())
               if instructions else None)
        return system_prompt(now, model, weights)

    session = Session(
        llm, hooks, prompt, session_log, settings,
        tool_context={
            "sandbox": sandbox, "outside": outside, "limits": limits,
            "web": web if web is not None else web_clients(limits),
            "own_files": own_files, "results": results,
            # The share of the context ZERO's own files take, as it stands,
            # for the memory tool's description
            "own_files_percent": lambda: limits["result_share_percent"],
            # The door to the skills it finds (read_skill), None for none
            "skills": skills,
            # The session's line in characters, as ZERO's own files count
            # them: the room found tools' schemas share (an MCP server's)
            "line_chars": lambda: (room.line(limits, server.context_window)
                                   * room.BYTES_PER_TOKEN),
        },
        rebuild=rebuild, code_mark=lambda: code_mark(session), fallback=fallback,
    )
    # A fork's log opens with its parent's header, whose settings a rebuild
    # restores: who answers this session's asks is its own, as built
    session.settings = {**session.settings, "judge": settings["judge"]}
    if "runs_as" in settings:
        session.settings["runs_as"] = runs_as
    else:
        session.settings.pop("runs_as", None)
    # What the file tools saw, from the session's log as it stands
    # (files/seen.py)
    session.tool_context["files"] = SeenFiles(lambda: session.log)
    # What it has used, for `usage` (a server adds its subagents')
    session.tool_context["session_use"] = SessionUse(lambda: session)
    # The tools found at run time it can call but doesn't send, for room,
    # which tool_search finds (found_tools/)
    session.tool_context["deferred_tools"] = (
        lambda: tools.deferred(session.tool_context))
    return session


def logged_settings(records):
    """The judge (judge_model and judge_thinking_effort, history's
    chosen_judge) and the effort a session's log says it runs with, so a
    session built again (a restart, a switch) keeps them whatever config.toml
    says now: (None, None) for a new one, and a judge of None where the log
    says nothing of it. Its limits aren't among them: every session reads
    them as they stand (an older log's copy is left unread)."""
    if not records or records[0].get("type") != "session":
        return None, None
    logged = history.settings_now(records)
    return history.chosen_judge(logged), logged.get("thinking_effort")


def server_session(paths, sid, info, log=None, ask=PARK, config=None, **options):
    """A harness session for the server: its asks park, no terminal to
    print to, its own log, and its workspace, writable in the sandbox (a
    workspace of / stays read-only). The user's SOUL.md, ZERO.md and saved
    permissions come from ZERO's config folder, with the project's ZERO.md.
    `config()`: ZERO's settings as they stand (settings.Config). Its judge
    and effort come from them when it is made, and from its log after that
    (logged_settings); an effort or a judge in `info` is a switch's (/model,
    the settings view), or a new session's first message's. Its limits and
    its fallback model are read from them at each use, with `limits` (the
    evals', tests') on top. `options` go to make_session and win over these."""
    log = log if log is not None else Log(paths.sessions / f"{sid}.jsonl")
    config = config or (lambda: settings.DEFAULTS)
    now = config()
    judge, effort = logged_settings(log.branch())
    if judge is None:
        judge = {
            "judge_model": now.get("judge_model"),
            "judge_thinking_effort": now.get(
                "judge_thinking_effort", settings.DEFAULTS["judge_thinking_effort"]
            ),
        }
    judge.update({name: info[name] for name in judge if name in info})
    limits = LiveSettings(config, fixed=options.pop("limits", {}))
    options = {
        "workspace": Path(info["workspace"]),
        "saved_permissions": permissions_file(),
        # A session keeps its judge as it keeps its model
        "judge_model": judge["judge_model"],
        "judge_thinking_effort": judge["judge_thinking_effort"],
        "thinking_effort": (
            info.get("thinking_effort") or effort
            or now.get("thinking_effort", settings.DEFAULTS["thinking_effort"])
        ),
        **options,
    }
    # Only when nothing else is given: the first use writes the user's files;
    # a session that reads the user's files finds the user's skills too
    if "instructions" not in options:
        options["instructions"] = user_instructions()
        options.setdefault(
            "skills", SkillFolders(options["workspace"], skills_folder()))
    session = make_session(
        info["model"], log=log, mode=info["mode"], ask=ask,
        limits=limits, unattended=info.get("started_by") == "zero", **options,
    )
    # A server carries it: notices and messages reach its running turn
    # (`wait` holds a turn for them)
    session.tool_context["on_a_server"] = True
    return session


def server_session_maker(paths, config, limits=None):
    """How the server makes a session (its `make_session`): server_session on
    `config()`, read as it stands at each use; `limits` over the config's."""
    return ServerSessionMaker(paths, config, {"limits": limits} if limits else {})


class ServerSessionMaker:
    """How the server makes a session (its `make_session`), and what it
    keeps of each: its scratch folder, kept while the server runs, so a
    session built again (a switch of mode, model or workspace) finds its
    files; let go of when the session is deleted (`forget`)."""

    def __init__(self, paths, config, options):
        self.paths = paths
        self.config = config
        self.options = options
        self.scratches = {}

    def __call__(self, sid, info, log=None, ask=PARK):
        return server_session(
            self.paths, sid, info, log, ask, config=self.config,
            scratch=self.scratch_for(sid), **self.options,
        )

    def scratch_for(self, sid):
        """The session's scratch folder, kept while the server runs; its name
        starts with the server's home's mark (sandbox's scratch_prefix), so
        the next server on the home removes it if this one is killed."""
        if sid not in self.scratches:
            self.scratches[sid] = Scratch(scratch_prefix(self.paths.home))
        return self.scratches[sid]

    def forget(self, sid):
        """A deleted session's scratch folder, let go of: removed once no
        harness of it holds it."""
        self.scratches.pop(sid, None)


def services(config, make_consent=None, saved_permissions=permissions_file,
             tick=2.0, mcp_servers=mcp_servers_file):
    """The components' services in the server (server/services.py).
    `make_consent(model)` builds the consent check for a session's model; by
    default on `config()`'s consent_silence_seconds, read at each check.
    `saved_permissions()`: the user's file of saved permissions; None for
    none (the evals). `tick`: how often jobs' check-ins are looked for.
    `mcp_servers()`: the user's list of MCP servers, whose tools the
    server's sessions are offered; None for none (the evals)."""
    if make_consent is None:
        def silence():
            return LiveSettings(config)["consent_silence_seconds"]

        def make_consent(model):
            return Consent(Model(model, timeout=silence))

    return [JobsService(tick=tick), PermissionsService(make_consent, saved_permissions),
            ModelService(), ContextService(), RoundsService(),
            SubagentsService(), SessionSearchService(),
            McpServersService(mcp_servers, LiveSettings(config)),
            SandboxService()]


# What ZERO needs from the machine, each need's check, declared here once
# (design rule 8; `zero doctor`): what is needed → check(), None when it is
# met, else what is wrong
MACHINE_CHECKS = {
    "The sandbox runs a command": check_sandbox,
}


def skill_notes(workspace):
    """What the skills found from `workspace` say that ZERO can't use as
    they are, a line each (`zero doctor`): a skill left out, a name found
    twice, a description cut (skills/found.py)."""
    return SkillFolders(workspace, skills_folder()).found().notes


def mcp_server_notes():
    """What the user's list of MCP servers says that ZERO can't use as it
    is, and each listed command not found on this machine (design rule 8:
    checked, not assumed), a line each (`zero doctor`)."""
    found = mcp_servers_listed.read(mcp_servers_file())
    notes = list(found.notes)
    for entry in found.entries:
        if entry.command and shutil.which(entry.command) is None:
            notes.append(f"MCP server {entry.name}: its command, "
                         f"{entry.command}, isn't found on this machine")
    return notes


def home_file_notes():
    """What ZERO's files in its home say that this ZERO doesn't know or
    can't use, a line each (`zero doctor`): config.toml's, models.toml's and
    keys.toml's, permissions.toml's, as each reader reports them."""
    notes = []
    said = io.StringIO()
    with contextlib.redirect_stderr(said):
        try:
            settings.load(notes=notes)
        except settings.SettingsError as error:
            notes.append(f"{error}; ZERO runs on the defaults")
        try:
            model_servers.table()
        except ModelServerError as error:
            notes.append(str(error))
        read_saved(permissions_file())
    notes += [line.removeprefix("ZERO: ") for line in said.getvalue().splitlines()
              if line.strip()]
    return notes
