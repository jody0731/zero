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

from core.asking import TO_APPROVER
from core.tools import ERROR, Result, tool
from skills import writer
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
    side_by_side=True,
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


# write_skill: what a call crosses, which asks in every mode, as a change to
# ZERO's own files does (memory/): a skill written reaches every later
# session, so a planted one would too, and someone answers for each
WRITE_CROSSING = (
    "It changes what every later session reads: your own skills are listed "
    "in each one's system message."
)
# How much of a skill an ask shows; the judge reads it whole
SHOWN_CHARS = 2000
WRITTEN = (
    "Wrote the skill {name} in {folder}{over}. Every session started from now "
    "lists it in its system message, under your own skills folder, with this "
    "description; read_skill reads it now. This session's system message "
    "stays as it was written. {files}"
)
OVER = ", over the one there"
NOT_WRITTEN = "Not written: {why}."
TAKEN = ("a skill named {name} is in {whose}, {folder}; a name another skills "
         "folder uses is taken")


def describe_write(limits, context=None):
    """write_skill's description, naming ZERO's own skills folder where the
    session has one."""
    found = (context or {}).get("skills")
    own = found.own if found is not None and found.own is not None else None
    where = f"your own skills folder, {own}" if own else "your own skills folder"
    return (
        f"Write a skill into {where}: a folder named `name` holding its "
        "SKILL.md, made from `description` (what the skill does and when it "
        "fits; the line the skills list shows) and `instructions` (its body), "
        "by the Agent Skills standard, and the files beside it (`files`: a "
        "path inside the folder for each, its text the value; null removes "
        "one; files a write doesn't name are kept, so a script, a template "
        "or a reference goes with the instructions that name it). A skill of "
        "that name there is written over; a name another skills folder uses "
        "is taken. Every session that starts from now lists it, and "
        "read_skill reads it at once; this session's system message stays as "
        f"it was written. Each write goes first to {TO_APPROVER}."
    )


def clipped(text, whole=False):
    text = str(text or "")
    if whole or len(text) <= SHOWN_CHARS:
        return text
    return f"{text[:SHOWN_CHARS]}… [{len(text) - SHOWN_CHARS:,} more characters]"


def write_summary(args, whole=False):
    """A write as an ask shows it and the judge reads it (`whole`)."""
    files = args.get("files") if isinstance(args.get("files"), dict) else {}
    lines = [f"write_skill {args.get('name', '')}: "
             f"{' '.join(str(args.get('description', '')).split())}",
             clipped(args.get("instructions"), whole)]
    for path, text in files.items():
        if text is None:
            lines.append(f"remove {path}")
        else:
            lines.append(f"{path}:\n{clipped(text, whole)}")
    return "\n".join(lines)


def write_crossing(args):
    return {"rule": "skill", "fact": WRITE_CROSSING, "why": args.get("reason"),
            "every_mode": True}


def can_write(context):
    found = context.get("skills")
    return found is not None and found.own is not None


@tool(
    describe_write,
    optional=("files", "compatibility", "reason"),
    summary=write_summary,
    ready=can_write,
    crossing=write_crossing,
    fields={"name": ("string", "The skill's name"),
            "folder": ("string", "The skill's folder")},
    name=("string", "The skill's name and its folder's: lowercase letters, "
                    "digits and single hyphens, 64 at most"),
    description=("string", "What the skill does and when it fits, 1,024 "
                           "characters at most"),
    instructions=("string", "The skill's instructions, its SKILL.md's body"),
    files=({"type": "object",
            "additionalProperties": {"type": ["string", "null"]}},
           "Files beside SKILL.md: a path inside the skill's folder for each, "
           "its text the value; null removes one"),
    compatibility=("string", "What the skill needs (a program, a network), "
                             "as the standard's compatibility field says it"),
    reason=("string", "Why the skill"),
)
def write_skill(name, description, instructions, files=None, compatibility=None,
                reason="", skills=None):
    """`skills`: the session's SkillFolders; ZERO's own folder is where a
    skill is written (writer.py)."""
    if skills is None or skills.own is None:
        return Result(ERROR + "this session has no skills folder of its own.",
                      failed=True)
    wanted = str(name or "").strip()
    other = next((s for s in skills.found().skills
                  if s.name == wanted and s.found_in != "zero"), None)
    if other is not None:
        why = TAKEN.format(name=wanted, whose=FROM[other.found_in],
                           folder=other.folder)
        return Result(NOT_WRITTEN.format(why=why), failed=True)
    try:
        folder, replaced = writer.write(skills.own, wanted, description,
                                        instructions, files, compatibility)
    except writer.NotWritten as why:
        return Result(NOT_WRITTEN.format(why=why), failed=True)
    except OSError as error:
        return Result(NOT_WRITTEN.format(why=error), failed=True)
    text = WRITTEN.format(name=wanted, folder=folder,
                          over=OVER if replaced else "",
                          files=files_line(folder))
    return Result(text, fields={"name": wanted, "folder": str(folder)})


TOOLS = (read_skill, write_skill)
