"""Component manifest for skills."""

from pathlib import Path

from core.components import ComponentManifest, SessionBuild
from paths import skills_folder
from components.skills.found import SkillFolders
from components.skills.hooks import Skills


def prepare_session(build: SessionBuild):
    if "skills" in build.options:
        skills = build.options["skills"]
    elif build.state.get("default_user_instructions"):
        skills = SkillFolders(build.workspace, skills_folder())
    else:
        skills = None

    # ZERO's own skills sit in its home, which every sandbox hides: their
    # folder is shown again, read-only, so their scripts run as any command.
    own_skills = skills.own if skills is not None else None
    if own_skills is not None:
        if (add_shown := build.state.get("add_sandbox_shown")) is not None:
            add_shown(own_skills)
        else:
            build.state.setdefault("sandbox_shown", []).append(own_skills)
        if (set_own := build.state.get("set_sandbox_own_skills")) is not None:
            set_own(own_skills)
        else:
            build.state["sandbox_own_skills"] = own_skills

    build.state["skills"] = skills
    build.tool_context["skills"] = skills


def session_hooks(build: SessionBuild):
    skills = build.state.get("skills")
    if skills is None:
        return []
    line_chars = build.state.get("line_chars") or build.tool_context.get(
        "line_chars"
    )
    return [Skills(skills, line_chars)]


def doctor(workspace=None):
    target = Path.cwd() if workspace is None else workspace
    notes = SkillFolders(target, skills_folder()).found().notes
    return [("!", note) for note in notes]


MANIFEST = ComponentManifest(
    name="skills",
    order=50,
    system_sources=("hooks.py",),
    prepare_session=prepare_session,
    session_hooks=session_hooks,
    doctor=doctor,
)
