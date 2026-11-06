"""The assembly: where ZERO is put together.

Each component declares its own settings, tools and plugins; this collects
them: every component's SETTINGS, in the settings view's ORDER; the tools, by
importing their modules in the order the model reads them; the plugins a
session loads in its mode, and their order (`chat`); a session and its tool
context (`make_session`); and what host/main.py hands the host kernel: how it
makes a session (`host_sessions`, `host_session`) and the components' parts
(`host_parts`).

A mode (permissions/modes.py) picks the sandbox and who decides an ask. The
judge never asks the user. With nobody there to ask (evals, tests: ask=None)
the user's asks are refused. The evals' host (dev/evalkit/) hands each session
its own box, the shipped SOUL.md and no saved permissions, and TimeCap by
max_seconds. Wherever anything asks, the soft caps load (limits/steps.py): a
turn past `max_steps` (50) asks to go on, as an 11th running job does. Who
ZERO and the user are comes from SOUL.md and ZERO.md (prompt/), never
from here.
`chat` is a function, not a list, because plugins keep state per session.
Order in the list matters for system-prompt text, and for a note one plugin
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
from model.host import ModelList
from model.providers import Model, resolve
from model.retry import ModelRetry
from limits import SETTINGS as LIMIT_SETTINGS
from limits.repeats import RepeatGuard
from limits.steps import StepLimit
from jobs import SETTINGS as JOB_SETTINGS
from jobs.host import Jobs
from jobs.plugin import JobLimit
from shell import SETTINGS as SHELL_SETTINGS
from files import SETTINGS as FILE_SETTINGS
from files.seen import ChangedFiles, FileMemory
from web import SETTINGS as WEB_SETTINGS
from web.tool import default_web
from judge import SETTINGS as JUDGE_SETTINGS
from judge.plugin import AutoApproval
from judge.verdict import Judge
from permissions import SETTINGS as PERMISSION_SETTINGS
from permissions.approval import (
    READ_ONLY, Approval, Permissions, Refuse, ask_user,
)
from permissions.asks import Asks, AskWhen
from permissions.consent import Consent
from permissions.grants import Remembered
from permissions.host import PermissionsHost
from permissions.modes import JUDGED, MODES, UNSANDBOXED, answers, canonical
from limits.stops import ContextGuard, FormatErrors, caps
from prompt.plugin import Environment
from prompt.instructions import PROJECT_FILE, read, system_prompt, users
from memory.plugin import OwnFiles
from paths import permissions_file
from sessions.log import Log, fresh_log
from sandbox.sandbox import (
    Outside, Sandbox, Scratch, Unsandboxed, private_folders,
)



# The settings view's order: each part declares its own settings (SETTINGS),
# and the view lists them as here
ORDER = (
    "mode", "model", "effort", "judge_model", "judge_effort", "max_steps",
    "step_grant", "max_jobs", "bash_timeout", "output_limit", "read_chars",
    "web_chars", "web_results", "web_timeout", "max_seconds", "context_stop",
    "model_timeout", "judge_timeout", "judge_retries", "judge_output_chars",
    "judge_outputs", "model_retries", "retry_base", "max_format_errors",
    "idle_minutes", "consent_timeout",
)
settings.add(
    *MODEL_SETTINGS, *LIMIT_SETTINGS, *JOB_SETTINGS, *SHELL_SETTINGS,
    *FILE_SETTINGS, *WEB_SETTINGS, *JUDGE_SETTINGS, *PERMISSION_SETTINGS,
    order=ORDER,
)

# A session's limits at their defaults, from the parts' settings, their one
# home: max_steps (a soft cap; None for none), max_seconds (a hard cap, off
# unless the evals set it), context_stop (None: the provider's own), the
# model's and judge's silence, retries, and bash's time and output
LIMITS = settings.limits()


def chat(
    limits,
    provider,
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
    plugins = [Environment(sandbox, answers(mode))]
    if own is not None:
        plugins.append(own)
    plugins += [
        ModelRetry(limits["model_retries"], limits["retry_base"]),
        ContextGuard(limits["context_stop"], provider.context),
    ]
    # The permissions memory where someone answers asks; before the
    # repeat guard, which lets a granted call through
    if spec.decides in ("user", "judge", "every call"):
        plugins.append(Remembered(saved, judged=mode in JUDGED))
    plugins += [
        RepeatGuard(),
        FormatErrors(limits["max_format_errors"]),
    ]
    if spec.asks:
        plugins.append(Asks())
    if ask_when is not None and spec.decides is not None:
        plugins.append(AskWhen(ask_when))
    # Soft caps ask wherever anything asks
    if spec.decides is not None:
        plugins.append(JobLimit())
        if limits["max_steps"] is not None:
            plugins.append(StepLimit(limits["max_steps"], limits["step_grant"]))
    if spec.decides == "nobody":
        plugins.append(Refuse(READ_ONLY))
    # With nobody to ask, the user's asks are refused
    elif spec.decides == "user" and ask is None:
        plugins.append(Refuse())
    elif spec.decides == "user":
        plugins.append(Approval(ask))
    elif spec.decides in ("judge", "every call"):
        # auto+ has no rules: what needs permission asks
        if spec.decides == "every call":
            plugins.append(Permissions())
        plugins.append(AutoApproval(judge))
    # bypassPermissions asks nothing but what a tool says asks in every mode
    # (a change to ZERO's own files)
    else:
        plugins.append(Asks(every_mode=True))
        plugins.append(Approval(ask) if ask is not None else Refuse())
    return plugins


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
    effort=None,
    web=None,
    judge_effort=None,
    box=None,
    scratch=None,
):
    """A session on the chat profile, plus the hard cap if `limits` sets it.

    `log`: the session's log (the host's); else a new one named in `folder`
    (tests, the evals' scripts).
    `ask`: how to ask the user; None when nobody is there (evals).
    `ask_when(call)`: True for a call that asks first, in a mode that asks
    (tests).
    `saved_permissions`: the user's file of saved permissions (the host
    passes paths.permissions_file()); None for none, as in the evals.
    `judge_*`: the auto modes' judge; `judge_model` None is the agent's own
    model, whatever it is switched to.
    `instructions`: what leads the system prompt (prompt/instructions.py:
    SOUL.md, the user's ZERO.md, the project's) and whether the memory tool
    may change it; None for nothing (tests).
    `workspace_writes`: False keeps the workspace read-only in every sandboxed
    mode (tests); a session's workspace is writable in default and auto,
    except a workspace of /, which the sandbox keeps read-only.
    `effort`: the agent's thinking (settings.py), sent with its own calls
    only; None sends nothing.
    `judge_effort`: the judge's thinking, sent with its calls only; None
    sends nothing, so the API's default holds.
    `web`: what the web tools reach the network with (web/tool.py); None
    builds the session's own (default_web).
    `box(workspace, private=)`: the sandbox every command runs in, made in
    place of sandbox/'s, whatever the mode: a mode with no sandbox of its
    own keeps it too (the judge isn't told), and no call leaves it. The evals
    hand in theirs (dev/evalkit/: the exam room, the throwaway machine), tests a
    plain Sandbox; None for the mode's own.
    `scratch`: the session's scratch folder (sandbox's Scratch) for the mode's
    own sandbox, which the host keeps across the session's builds; None for
    one of the sandbox's own.
    """
    if workspace is None:
        raise ValueError("a session needs a workspace")
    mode = canonical(mode)
    spec = MODES[mode]
    limits = {**LIMITS, **(limits or {})}
    provider, _ = resolve(model)
    if limits["context_stop"] is None:
        limits["context_stop"] = provider.context_stop
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
        own = OwnFiles(own_files, found.others, sandbox.workspace)
    judge = None
    if mode in JUDGED:
        judge = Judge(
            judge_model or model, judge_client, timeout=limits["judge_timeout"],
            retries=limits["judge_retries"], effort=judge_effort,
            output_chars=limits["judge_output_chars"],
            outputs=limits["judge_outputs"],
        )
    settings = {
        **limits,
        "provider": provider.name,
        "context_limit": provider.context,
        # USD per 1M tokens at peak (hit, miss, output); evals estimate cost
        "price": provider.price,
        "mode": mode,
        "sandbox": sandbox.sandboxed,
        "workspace": str(sandbox.workspace),
        "instructions": found.record() if found is not None else None,
        "out_of_view": [str(p) for p in getattr(sandbox, "covered", list)()],
        # The judge as the user left it, whatever the mode; and what it runs
        # with in this one (None: no judge in this mode)
        "judge_model": judge_model,
        "judge_effort": judge_effort,
        "judge": (
            {"model": judge.model, "effort": judge.effort} if judge else None
        ),
        "kept_sandbox": box is not None and mode in UNSANDBOXED,
        "can_leave_sandbox": outside is not None,
        "effort": effort,
    }
    plugins = chat(
        limits, provider, sandbox, mode, ask, ask_when, judge, outside,
        saved_permissions, own,
    )
    plugins += caps(limits)
    # On a resume, the files the cut-off turn touched that changed meanwhile
    plugins.append(ChangedFiles())
    llm = Model(model, client, timeout=limits["model_timeout"], effort=effort)
    if log is None and folder is None:
        raise ValueError("a session needs its log, or a folder to start one in")
    session_log = log if log is not None else fresh_log(folder, model)
    prompt = system_prompt(found, model)
    session = Session(
        llm, plugins, prompt, session_log, settings,
        tool_context={
            "sandbox": sandbox, "outside": outside, "limits": limits,
            "web": web if web is not None else default_web(limits),
            "own_files": own_files,
        },
    )
    # What the file tools saw, from the session's log as it stands
    # (files/seen.py)
    session.tool_context["files"] = FileMemory(lambda: session.log)
    return session


def made_with(records):
    """The limits, the judge (judge_model and judge_effort, history's
    chosen_judge) and the effort a session's log says it runs with, so a
    session built again (a restart, a switch) keeps them whatever config.toml
    says now: ({}, None, None) for a new one, and a judge of None where the
    log says nothing of it. Not the context stop: the log has the one its
    model's provider gave, and a switch may change the model."""
    if not records or records[0].get("type") != "session":
        return {}, None, None
    logged = history.settings_now(records)
    limits = {
        name: logged[name] for name in LIMITS
        if name in logged and name != "context_stop"
    }
    return limits, history.chosen_judge(logged), logged.get("effort")


def host_session(paths, sid, info, log=None, ask=PARK, config=None, **options):
    """A harness session for the host: its asks park, no terminal to
    print to, its own log, and its workspace, writable in the sandbox (a
    workspace of / stays read-only). The user's SOUL.md, ZERO.md and saved
    permissions come from ZERO's config folder, with the project's ZERO.md;
    its limits, judge and effort from `config` (config.toml's settings) when
    it is made, and from its log after that (made_with); an effort or a judge
    in `info` is a switch's (/model, the settings view), or a new session's
    first message's. `options` go to make_session and win over these."""
    log = log if log is not None else Log(paths.sessions / f"{sid}.jsonl")
    config = config or {}
    limits, judge, effort = made_with(log.records())
    if judge is None:
        judge = {
            "judge_model": config.get("judge_model"),
            "judge_effort": config.get(
                "judge_effort", settings.DEFAULTS["judge_effort"]
            ),
        }
    judge.update({name: info[name] for name in judge if name in info})
    limits = {
        **{name: config[name] for name in LIMITS if name in config},
        **limits,
        **options.pop("limits", {}),
    }
    options = {
        "workspace": Path(info["workspace"]),
        "saved_permissions": permissions_file(),
        # A session keeps its judge as it keeps its model
        "judge_model": judge["judge_model"],
        "judge_effort": judge["judge_effort"],
        "effort": (
            info.get("effort") or effort
            or config.get("effort", settings.DEFAULTS["effort"])
        ),
        **options,
    }
    # Only when nothing else is given: the first use writes the user's files
    if "instructions" not in options:
        options["instructions"] = users()
    return make_session(
        info["model"], log=log, mode=info["mode"], ask=ask,
        limits=limits, **options,
    )


def host_sessions(paths, config, limits=None):
    """How the host makes a session (its `make_session`): host_session on
    `config()` as it stands; `limits` over the config's."""
    options = {"limits": limits} if limits else {}
    # Each session's scratch folder, kept while the host runs: a session
    # built again (a switch of mode, model or workspace) finds its files
    scratches = {}

    def make(sid, info, log=None, ask=PARK):
        scratch = scratches.setdefault(sid, Scratch())
        return host_session(
            paths, sid, info, log, ask, config=config(), scratch=scratch,
            **options,
        )

    return make


def host_parts(config, make_consent=None, saved_permissions=permissions_file,
               tick=2.0):
    """The components' parts in the host (host/parts.py). `make_consent(model)`
    builds the consent check for a session's model; by default on `config()`'s
    consent_timeout. `saved_permissions()`: the user's file of saved
    permissions; None for none (the evals). `tick`: how often jobs' check-ins
    are looked for."""
    if make_consent is None:
        def make_consent(model):
            return Consent(Model(model, timeout=config()["consent_timeout"]))

    return [Jobs(tick=tick), PermissionsHost(make_consent, saved_permissions),
            ModelList()]
