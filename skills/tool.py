"""read_skill: a skill's instructions in full, by the name the system message
lists it under (the standard's guide: a tool that reads it, where a plain
file read would do, so the text comes marked). Its SKILL.md is read as it
stands, its frontmatter left out, between <skill> tags that say whose it is
and where its folder is; after them, what its author says it needs, and its
files, named, not read. A skill installed since the system message was
written is found too. The text is never cleared to make room (core/tools.py,
`never_cleared`): the standard's guide keeps a skill's instructions out of
pruning, as a session goes on following them.
"""

from html import escape

from core.tools import ERROR, Result, tool
from skills.found import SkillProblem, files_of, instructions_of

DESCRIPTION = (
    "A skill's instructions in full: its SKILL.md as its author wrote it, by "
    "the name the system message lists it under; with its folder, which the "
    "paths it names are relative to, and the files there."
)
# Whose a skill is, by the kind of folder it was found in
FROM = {
    "project": "the project's skills folder",
    "zero": "your own skills folder",
    "user": "the user's skills folder",
}
# The skill's text, between tags that say whose it is
SKILL = '<skill name="{name}" from="{whose}" folder="{folder}">\n{text}\n</skill>'
NEEDS = "Its author says it needs: {needs}"
FOLDER = ("Its folder is {folder}: the paths its instructions name are "
          "relative to it. {files}")
FILES = "Its files: {files}."
MORE_FILES = "{files}, and {more} more"
NO_FILES = "It holds its SKILL.md alone."
UNKNOWN = "no skill named {name!r} is installed. Skills found now: {names}."
NONE_FOUND = "no skill named {name!r} is installed, nor any other."
UNUSABLE = "the skill {name} can't be read now: {problem}."


def files_line(folder):
    """The skill's files, as the folder line names them."""
    listed, more = files_of(folder)
    if not listed:
        return NO_FILES
    names = ", ".join(listed)
    if more:
        names = MORE_FILES.format(files=names, more=more)
    return FILES.format(files=names)


@tool(
    DESCRIPTION,
    summary=lambda args: f"read_skill {args.get('name', '')}",
    ready=lambda context: context.get("skills") is not None,
    needs_permission=lambda args: False,
    never_cleared=True,
    fields={"name": ("string", "The skill's name"),
            "folder": ("string", "The skill's folder")},
    name=("string", "The skill's name, as the system message lists it"),
)
def read_skill(name, skills=None):
    found = skills.found()
    skill = found.named(str(name).strip())
    if skill is None:
        names = ", ".join(s.name for s in found.skills)
        text = (UNKNOWN.format(name=name, names=names) if names
                else NONE_FOUND.format(name=name))
        return Result(ERROR + text, failed=True)
    try:
        instructions, needs = instructions_of(skill)
    except SkillProblem as problem:
        text = UNUSABLE.format(name=skill.name, problem=problem)
        return Result(ERROR + text, failed=True)
    parts = [SKILL.format(name=escape(skill.name), whose=FROM[skill.found_in],
                          folder=escape(str(skill.folder)), text=instructions)]
    if needs:
        parts.append(NEEDS.format(needs=needs))
    parts.append(FOLDER.format(folder=skill.folder,
                               files=files_line(skill.folder)))
    return Result("\n".join(parts),
                  fields={"name": skill.name, "folder": str(skill.folder)})


TOOLS = (read_skill,)
