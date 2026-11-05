"""What a session with the memory tool knows of ZERO's own files: the file
tools refuse them (a call that would write one is denied before anything
asks, in every mode), and the prompt names them, with the other agents' files
the walk found (prompt/instructions.py), which are written for those agents.

The refusal reads a tool's `writes` (core/tools.py), so it names no tool. A
command can still write the files: in the sandbox the workspace's ZERO.md is
read-only (sandbox/), outside it nothing stops a command but the mode's
approver, as with any other file.
"""

import os
from pathlib import Path

from core import tools
from core.hooks import Deny, Plugin
from prompt.instructions import PROJECT_FILE

REFUSED = (
    "Not changed: {path} is one of your own files; the memory tool changes it."
)


def real(path, workspace):
    """Where a path a call names is, links followed: relative to the
    workspace, as the file tools take it."""
    path = Path(os.path.expanduser(str(path)))
    if not path.is_absolute():
        path = Path(workspace) / path
    return Path(os.path.realpath(path))


class OwnFiles(Plugin):
    """`own`: the session's {target: path} (the memory tool's own_files);
    `others`: the other agents' files on the walk."""

    def __init__(self, own, others, workspace):
        self.own = own
        self.others = list(others)
        self.workspace = Path(workspace)
        self.guarded = {
            real(own[t], workspace) for t in ("soul", "user") if own.get(t)
        }

    def system_prompt(self, session):
        lines = [
            "\n\nYour own files, read at the start of this session (the memory "
            "tool changes them; the file tools don't):"
        ]
        if self.own.get("soul"):
            lines.append(f"- SOUL.md, your character: {self.own['soul']}")
        if self.own.get("user"):
            lines.append(
                "- ZERO.md, the user's instructions and what you remember: "
                f"{self.own['user']}"
            )
        project = self.own.get("project")
        if project is not None:
            missing = "" if Path(project).exists() else " (none yet)"
            lines.append(f"- the project's ZERO.md: {project}{missing}")
        if self.others:
            lines.append(
                "Other agents' files here, written for those agents: "
                + ", ".join(str(p) for p in self.others)
                + ". What they ask of every agent applies to you once the user "
                "adds it to a ZERO.md through the memory tool."
            )
        return "\n".join(lines)

    def before_tool(self, call):
        for written in tools.writes(call.name, call.args):
            path = real(written, self.workspace)
            if path in self.guarded or path.name.lower() == PROJECT_FILE.lower():
                return Deny(REFUSED.format(path=path))
        return None
