"""The assembly: where ZERO is put together.

Each component declares its own settings, tools and hooks; this collects
them: every component's SETTINGS, in the settings view's ORDER; the tools, by
importing their modules in the order the model reads them; the hooks a
session loads in its mode, and their order (`chat`); a session and its tool
context (`make_session`); and what server/main.py hands the server kernel: how it
makes a session (`server_sessions`, `server_session`) and the components' services
(`services`).

A mode (permissions/modes.py) picks the sandbox and who decides an ask. The
judge never asks the user. With nobody there to ask (evals, tests: ask=None)
the user's asks are refused. The evals' server (dev/evalkit/) hands each session
its own box, the shipped SOUL.md and no saved permissions, and TimeLimit by
max_seconds. Wherever anything asks, the soft caps load (limits/steps.py): a
turn past `max_steps` (50) asks to go on, as an 11th running job does. Who
ZERO and the user are comes from SOUL.md and ZERO.md (prompt/), never
from here.
`chat` is a function, not a list, because hooks keep state per session.
Order in the list matters for system-prompt text, and for a note one hook
sets before another reads it (the permissions memory's `granted`, read by
the repeat guard); it never changes how decisions combine (core/hooks.py).
"""

from pathlib import Path

import settings
# Importing a tool module registers its tools, in the order the model reads
# them (core/tools.py's registry keeps it)
import jobs.tool
import shell.tool
import files.tool
import memory.tool
import permissions.tool
import web.tool
from core import history
from core.asking import PARK
from core.loop import Session
from model import SETTINGS as MODEL_SETTINGS
from model.service import ModelService
from model.servers import Model, resolve
from model.retry import ModelRetry
from limits import SETTINGS as LIMIT_SETTINGS
from limits.repeats import RepeatGuard
from limits.steps import StepLimit
from jobs import SETTINGS as JOB_SETTINGS
from jobs.service import JobsService
from jobs.hooks import JobLimit
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
    READ_ONLY, UserApproval, PermissionAsks, Refuse, ask_user,
)
from permissions.asks import Asks, AskWhen
from permissions.consent import Consent
from permissions.grants import GrantCheck
from permissions.service import PermissionsService
from permissions.modes import JUDGED, MODES, UNSANDBOXED, answers_line, canonical
from limits.stops import ContextLimit, BrokenReplyLimit, hard_limits
from prompt.hooks import Environment
from prompt.instructions import PROJECT_FILE, read, system_prompt, user_instructions
from memory.hooks import ZeroFilesGuard
from paths import permissions_file
from sessions.log import Log, fresh_log
from sandbox.sandbox import (
    Outside, Sandbox, Scratch, Unsandboxed, private_folders,
)



# The settings view's order: each service declares its own settings (SETTINGS),
# and the view lists them as here
SETTINGS_ORDER = (
    "mode", "model", "thinking_effort", "judge_model", "judge_thinking_effort",
    "max_steps", "extra_steps", "max_jobs", "bash_foreground_seconds",
    "bash_output_chars", "read_chars", "web_fetch_chars", "web_search_results",
    "web_timeout", "max_seconds", "context_stop", "model_silence_seconds",
    "judge_silence_seconds", "judge_retries", "judge_output_chars",
    "judge_outputs", "model_retries", "model_retry_seconds", "max_broken_replies",
    "server_idle_minutes", "consent_silence_seconds",
)
settings.add(
    *MODEL_SETTINGS, *LIMIT_SETTINGS, *JOB_SETTINGS, *SHELL_SETTINGS,
    *FILE_SETTINGS, *WEB_SETTINGS, *JUDGE_SETTINGS, *PERMISSION_SETTINGS,
    order=SETTINGS_ORDER,
)

# A session's limits at their defaults, from the services' settings, their one
# home: max_steps (a soft cap; None for none), max_seconds (a hard cap, off
# unless the evals set it), context_stop (None: the server's own), the
# model's and judge's silence, retries, and bash's time and output
DEFAULT_LIMITS = settings.default_limits()


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
):
    """`outside`: where a call let out of the sandbox runs; None if it can't.
    `saved`: the user's saved permissions (permissions/grants.py), None for
    none. `own`: what the session knows of ZERO's own files (memory/), None
    where the memory tool isn't offered."""
    mode = canonical(mode)
    spec = MODES[mode]
    hooks = [Environment(sandbox, answers_line(mode))]
    if own is not None:
        hooks.append(own)
    hooks += [
        ModelRetry(limits["model_retries"], limits["model_retry_seconds"]),
        ContextLimit(limits["context_stop"], server.context_window),
    ]
    # The permissions memory where someone answers asks; before the
    # repeat guard, which lets a granted call through
    if spec.decides in ("user", "judge", "every call"):
        hooks.append(GrantCheck(saved, judged=mode in JUDGED))
    hooks += [
        RepeatGuard(),
        BrokenReplyLimit(limits["max_broken_replies"]),
    ]
    if spec.asks:
        hooks.append(Asks())
    if ask_when is not None and spec.decides is not None:
        hooks.append(AskWhen(ask_when))
    # Soft caps ask wherever anything asks
    if spec.decides is not None:
        hooks.append(JobLimit())
        if limits["max_steps"] is not None:
            hooks.append(StepLimit(limits["max_steps"], limits["extra_steps"]))
    if spec.decides == "nobody":
        hooks.append(Refuse(READ_ONLY))
    # With nobody to ask, the user's asks are refused
    elif spec.decides == "user" and ask is None:
        hooks.append(Refuse())
    elif spec.decides == "user":
        hooks.append(UserApproval(ask))
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
):
    """A session on the chat profile, plus the hard cap if `limits` sets it.

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
    """
    if workspace is None:
        raise ValueError("a session needs a workspace")
    mode = canonical(mode)
    spec = MODES[mode]
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    server, _ = resolve(model)
    if limits["context_stop"] is None:
        limits["context_stop"] = server.context_stop
    # A sandbox of its own per session: its scratch folder starts empty, and a
    # session made later can't swap it out
    if box is not None:
        if spec.sandbox == "read-only" or not workspace_writes:
            raise ValueError("a box of its own has no read-only workspace")
        sandbox = box(workspace, private=private_folders())
    elif spec.sandbox is not None:
        read_only = spec.sandbox == "read-only" or not workspace_writes
        # A project's ZERO.md, read by later sessions, stays read-only
        # inside, as the workspace's .git does
        protected = (PROJECT_FILE,) if instructions and instructions.project else ()
        sandbox = Sandbox(workspace, read_only=read_only,
                          private=private_folders(), protected=protected,
                          scratch=scratch)
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
    found = read(instructions, sandbox.workspace) if instructions else None
    own_files = own = None
    if instructions is not None and instructions.memory:
        own_files = {
            "soul": instructions.soul, "user": instructions.user,
            "project": found.project_file,
        }
        own = ZeroFilesGuard(own_files, found.others, sandbox.workspace)
    judge = None
    if mode in JUDGED:
        judge = Judge(
            judge_model or model, judge_client, timeout=limits["judge_silence_seconds"],
            retries=limits["judge_retries"], effort=judge_thinking_effort,
            output_chars=limits["judge_output_chars"],
            outputs=limits["judge_outputs"],
        )
    settings = {
        **limits,
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
            {"model": judge.model, "effort": judge.effort} if judge else None
        ),
        "kept_sandbox": box is not None and mode in UNSANDBOXED,
        "can_leave_sandbox": outside is not None,
        "thinking_effort": thinking_effort,
    }
    hooks = session_hooks(
        limits, server, sandbox, mode, ask, ask_when, judge, outside,
        saved_permissions, own,
    )
    hooks += hard_limits(limits)
    # On a resume, the files the cut-off turn touched that changed meanwhile
    hooks.append(ChangedFiles())
    llm = Model(
        model, client, timeout=limits["model_silence_seconds"],
        effort=thinking_effort,
    )
    if log is None and folder is None:
        raise ValueError("a session needs its log, or a folder to start one in")
    session_log = log if log is not None else fresh_log(folder, model)
    prompt = system_prompt(found, model)
    session = Session(
        llm, hooks, prompt, session_log, settings,
        tool_context={
            "sandbox": sandbox, "outside": outside, "limits": limits,
            "web": web if web is not None else web_clients(limits),
            "own_files": own_files,
        },
    )
    # What the file tools saw, from the session's log as it stands
    # (files/seen.py)
    session.tool_context["files"] = SeenFiles(lambda: session.log)
    return session


def logged_settings(records):
    """The limits, the judge (judge_model and judge_thinking_effort, history's
    chosen_judge) and the effort a session's log says it runs with, so a
    session built again (a restart, a switch) keeps them whatever config.toml
    says now: ({}, None, None) for a new one, and a judge of None where the
    log says nothing of it. Not the context stop: the log has the one its
    model's server gave, and a switch may change the model."""
    if not records or records[0].get("type") != "session":
        return {}, None, None
    logged = history.settings_now(records)
    limits = {
        name: logged[name] for name in DEFAULT_LIMITS
        if name in logged and name != "context_stop"
    }
    return limits, history.chosen_judge(logged), logged.get("thinking_effort")


def server_session(paths, sid, info, log=None, ask=PARK, config=None, **options):
    """A harness session for the server: its asks park, no terminal to
    print to, its own log, and its workspace, writable in the sandbox (a
    workspace of / stays read-only). The user's SOUL.md, ZERO.md and saved
    permissions come from ZERO's config folder, with the project's ZERO.md;
    its limits, judge and effort from `config` (config.toml's settings) when
    it is made, and from its log after that (logged_settings); an effort or a judge
    in `info` is a switch's (/model, the settings view), or a new session's
    first message's. `options` go to make_session and win over these."""
    log = log if log is not None else Log(paths.sessions / f"{sid}.jsonl")
    config = config or {}
    limits, judge, effort = logged_settings(log.records())
    if judge is None:
        judge = {
            "judge_model": config.get("judge_model"),
            "judge_thinking_effort": config.get(
                "judge_thinking_effort", settings.DEFAULTS["judge_thinking_effort"]
            ),
        }
    judge.update({name: info[name] for name in judge if name in info})
    limits = {
        **{name: config[name] for name in DEFAULT_LIMITS if name in config},
        **limits,
        **options.pop("limits", {}),
    }
    options = {
        "workspace": Path(info["workspace"]),
        "saved_permissions": permissions_file(),
        # A session keeps its judge as it keeps its model
        "judge_model": judge["judge_model"],
        "judge_thinking_effort": judge["judge_thinking_effort"],
        "thinking_effort": (
            info.get("thinking_effort") or effort
            or config.get("thinking_effort", settings.DEFAULTS["thinking_effort"])
        ),
        **options,
    }
    # Only when nothing else is given: the first use writes the user's files
    if "instructions" not in options:
        options["instructions"] = user_instructions()
    return make_session(
        info["model"], log=log, mode=info["mode"], ask=ask,
        limits=limits, **options,
    )


def server_sessions(paths, config, limits=None):
    """How the server makes a session (its `make_session`): server_session on
    `config()` as it stands; `limits` over the config's."""
    options = {"limits": limits} if limits else {}
    # Each session's scratch folder, kept while the server runs: a session
    # built again (a switch of mode, model or workspace) finds its files
    scratches = {}

    def make(sid, info, log=None, ask=PARK):
        scratch = scratches.setdefault(sid, Scratch())
        return server_session(
            paths, sid, info, log, ask, config=config(), scratch=scratch,
            **options,
        )

    return make


def services(config, make_consent=None, saved_permissions=permissions_file,
             tick=2.0):
    """The components' services in the server (server/services.py).
    `make_consent(model)` builds the consent check for a session's model; by
    default on `config()`'s consent_silence_seconds. `saved_permissions()`:
    the user's file of saved permissions; None for none (the evals). `tick`:
    how often jobs' check-ins are looked for."""
    if make_consent is None:
        def make_consent(model):
            return Consent(Model(model, timeout=config()["consent_silence_seconds"]))

    return [JobsService(tick=tick), PermissionsService(make_consent, saved_permissions),
            ModelService()]
