"""Component manifest for prompt."""

import json
from pathlib import Path

import settings
from core.components import ComponentManifest, SessionBuild
from components.prompt import SETTINGS
from components.prompt.hooks import Environment, Means, means_lines
from components.prompt.instructions import (
    PROJECT_FILE,
    read,
    system_prompt,
    text as instruction_text,
    user_instructions,
)
from components.prompt.keywords import definitions


def prepare_session(build: SessionBuild):
    # Only when nothing else is given on the server: the first use writes the
    # user's files, and a session that reads them finds the user's skills too.
    if "instructions" in build.options:
        instructions = build.options["instructions"]
    elif build.options.get("on_a_server"):
        instructions = user_instructions()
        build.state["default_user_instructions"] = True
    else:
        instructions = None

    # A project's ZERO.md, read by later sessions, stays read-only inside the
    # sandbox, as the workspace's .git does.
    if instructions and instructions.project:
        if (add_protected := build.state.get("add_sandbox_protected")) is not None:
            add_protected(PROJECT_FILE)
        else:
            build.state.setdefault("sandbox_protected", []).append(PROJECT_FILE)

    sandbox = build.tool_context.get("sandbox")
    workspace = (
        sandbox.workspace
        if sandbox is not None
        else Path(build.workspace).resolve()
    )
    most_chars = build.state.get("most_chars")
    limit_chars = most_chars() if most_chars is not None else 0
    found = read(instructions, workspace, limit_chars) if instructions else None

    weights = build.state.get("token_weights")
    build.state["instructions"] = instructions
    build.state["found_instructions"] = found
    build.state["user_keywords"] = lambda: definitions(
        instruction_text(instructions.user)
        if instructions and instructions.user
        else ""
    )
    build.settings["instructions"] = found.record() if found is not None else None
    build.state["system_prompt"] = system_prompt(found, build.model, weights)

    def rebuild():
        # The user's files and the project's, read again as they stand now.
        current_limit = most_chars() if most_chars is not None else 0
        now = (
            read(instructions, workspace, current_limit)
            if instructions
            else None
        )
        return system_prompt(now, build.model, weights)

    build.state["rebuild_system_prompt"] = rebuild


def session_hooks(build: SessionBuild):
    sandbox = build.tool_context.get("sandbox")
    weights = build.state.get("token_weights")
    mode_line = build.state.get("answers_line")
    return [
        Means(build.limits, weights),
        Environment(sandbox, mode_line),
    ]


def _code_mark_part(session):
    limits = session.tool_context.get("limits") or settings.DEFAULTS
    return (
        json.dumps(means_lines(session.tool_context)).encode()
        + limits["time_or_tokens"].encode()
    )


def after_session(session, build: SessionBuild):
    session.tool_context.setdefault("code_mark_parts", []).append(_code_mark_part)


MANIFEST = ComponentManifest(
    name="prompt",
    order=40,
    settings=SETTINGS,
    system_sources=(
        "instructions.py",
        "hooks.py",
        "keywords.py",
    ),
    prepare_session=prepare_session,
    session_hooks=session_hooks,
    after_session=after_session,
)
