"""Component manifest for memory."""

from core.components import ComponentManifest, SessionBuild
from components.memory.hooks import ZeroFilesGuard


def prepare_session(build: SessionBuild):
    instructions = build.state.get("instructions")
    found = build.state.get("found_instructions")
    sandbox = build.tool_context.get("sandbox")
    workspace = sandbox.workspace if sandbox is not None else build.workspace
    own_files = None
    own = None
    if instructions is not None and instructions.memory and found is not None:
        own_files = {
            "soul": instructions.soul,
            "user": instructions.user,
            "project": found.project_file,
        }
        own = ZeroFilesGuard(own_files, found.others, workspace)
    build.tool_context["own_files"] = own_files
    build.state["own_files_guard"] = own


def session_hooks(build: SessionBuild):
    own = build.state.get("own_files_guard")
    return [own] if own is not None else []


MANIFEST = ComponentManifest(
    name="memory",
    order=45,
    system_sources=("hooks.py",),
    prepare_session=prepare_session,
    session_hooks=session_hooks,
)
