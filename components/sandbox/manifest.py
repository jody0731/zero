"""Component manifest for sandbox."""

from core.components import ComponentManifest, ServiceBuild, SessionBuild
from components.sandbox import check as sandbox_check
from components.sandbox.sandbox import (
    Outside,
    Sandbox,
    Scratch,
    Unsandboxed,
    private_folders,
    scratch_prefix,
)
from components.sandbox.service import SandboxService

SANDBOX_NEED = "The sandbox runs a command"


def make_scratch(home):
    """Create a session scratch folder marked with the server's home."""
    return Scratch(scratch_prefix(home))


def prepare_session(build: SessionBuild):
    spec = build.state.get("mode_spec")
    sandbox_kind = spec.sandbox if spec is not None else "workspace"
    box = build.options.get("box")
    scratch = build.options.get("scratch")
    workspace_writes = build.options.get("workspace_writes", True)
    shown = list(build.state.get("sandbox_shown", ()))
    protected = tuple(build.state.get("sandbox_protected", ()))
    own_skills = build.state.get("sandbox_own_skills")

    # A sandbox of its own per session: its scratch folder starts empty, and a
    # session made later can't swap it out.
    if box is not None:
        if sandbox_kind == "read-only" or not workspace_writes:
            raise ValueError("a box of its own has no read-only workspace")
        sandbox = box(build.workspace, private=private_folders())
        sandbox.shown.extend(shown)
        sandbox.own_skills = own_skills
    elif sandbox_kind is not None:
        read_only = sandbox_kind == "read-only" or not workspace_writes
        sandbox = Sandbox(
            build.workspace,
            read_only=read_only,
            private=private_folders(),
            protected=protected,
            scratch=scratch,
            shown=shown,
            own_skills=own_skills,
        )
    else:
        sandbox = Unsandboxed(build.workspace)

    if isinstance(sandbox, Sandbox):
        read_only_sandbox = sandbox.without_changes()
    elif box is None:
        read_only_sandbox = Sandbox(
            build.workspace,
            no_changes=True,
            private=private_folders(),
            scratch=scratch,
            shown=shown,
            own_skills=own_skills,
        )
    else:
        # A custom execution environment must supply its own confinement.
        read_only_sandbox = None

    # Where one call runs once its ask to leave the sandbox is approved: only
    # in a mode whose own sandbox has the workspace, never from a box handed in.
    outside = None
    if sandbox_kind == "workspace" and box is None:
        outside = Outside(build.workspace, sandbox=sandbox)

    boxes = (sandbox, read_only_sandbox)

    def add_shown(item):
        for target in boxes:
            if target is not None and hasattr(target, "shown"):
                if item not in target.shown:
                    target.shown.append(item)

    def add_protected(name):
        for target in boxes:
            if isinstance(target, Sandbox) and name not in target.protected:
                target.protected = (*target.protected, name)

    def set_own_skills(path):
        for target in boxes:
            if target is not None and hasattr(target, "own_skills"):
                target.own_skills = path

    build.state["sandbox"] = sandbox
    build.state["read_only_sandbox"] = read_only_sandbox
    build.state["outside"] = outside
    build.state["add_sandbox_shown"] = add_shown
    build.state["add_sandbox_protected"] = add_protected
    build.state["set_sandbox_own_skills"] = set_own_skills

    build.settings["sandbox"] = sandbox.sandboxed
    build.settings["workspace"] = str(sandbox.workspace)
    build.settings["out_of_view"] = [
        str(p) for p in getattr(sandbox, "covered_folders", list)()
    ]
    build.settings["kept_sandbox"] = box is not None and sandbox_kind is None
    build.settings["can_leave_sandbox"] = outside is not None

    build.tool_context["sandbox"] = sandbox
    build.tool_context["outside"] = outside
    build.tool_context["read_only_sandbox"] = read_only_sandbox


def service(build: ServiceBuild):
    return SandboxService()


def doctor(workspace=None):
    found = sandbox_check.check_sandbox()
    if found is None:
        return [("✓", SANDBOX_NEED)]
    return [("✗", f"{SANDBOX_NEED}: no; {found}")]


MANIFEST = ComponentManifest(
    name="sandbox",
    order=30,
    system_sources=("sandbox.py",),
    prepare_session=prepare_session,
    service=service,
    doctor=doctor,
)
