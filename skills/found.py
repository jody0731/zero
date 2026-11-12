"""Where skills are found, and what a SKILL.md holds.

Three kinds of folder, nearest first: the project's `.agents/skills`, from
the workspace up to the project's root (the walk prompt/ takes for ZERO.md);
ZERO's own, in its home; the user's `~/.agents/skills`, which the agents on
the machine share. A name found twice is taken from the nearer folder (the
standard's guide: a project's over the user's), the other noted. Another
agent's own folder (`~/.claude/skills`) is not read: that agent's folder
holds its logins, which the sandbox hides, and a skill for every agent goes
in the shared one.

A skill is a folder one level down in a skills folder (or a link to one)
holding a SKILL.md: YAML frontmatter between `---` lines, its instructions
after. Read leniently, as the standard's guide asks: a skill with no
description, or frontmatter that isn't YAML even with `key: a: b` values
quoted, is left out; a name unlike the standard's form or its folder's, or a
description past the standard's 1,024 characters (cut there), is used. Both
come with a note for the user (`zero doctor`); the model sees only skills it
can use. A skill its author kept for the user to start
(`disable-model-invocation`) is left out with a note: ZERO starts none for
the user.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

import paths
from prompt.instructions import project_root, walk

# The file a skill's folder holds, and the standard's skills folder, in a
# project and in the user's home
SKILL_FILE = "SKILL.md"
STANDARD_FOLDER = Path(".agents") / "skills"
# Each kind of skills folder, nearest first
FOUND_IN = ("project", "zero", "user")
# The standard's limits on a description and on its compatibility line
DESCRIPTION_CHARS = 1024
COMPATIBILITY_CHARS = 500
# The standard's form of a name: lowercase letters, digits and single
# hyphens inside, 64 characters at most
NAME_FORM = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
NAME_CHARS = 64
# A SKILL.md past this is left out: no skill's instructions run so long
# (the standard asks for under 500 lines), and each look reads every one
MOST_FILE_BYTES = 256 * 1024
# The frontmatter: between a `---` line at the file's start and the next
FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)^---[ \t]*(?:\r?\n|\Z)",
                         re.DOTALL | re.MULTILINE)
# A top-level field on one line, its value plain (not quoted, a block or a
# list), which other readers take with ": " inside it
PLAIN_FIELD = re.compile(r"^([A-Za-z0-9_-]+):[ \t]+([^\"'|>\[{&*!#\s].*?)[ \t]*$")
# Folders in a skill's folder that hold no file of the skill's own
NOT_THE_SKILLS = {"__pycache__", "node_modules"}
# The files a skill's folder is listed with, at most, and the entries
# looked through for them
FILES_LISTED = 50
FILES_LOOKED_AT = 2000


@dataclass(frozen=True)
class Skill:
    """A skill found: its `name` and `description` (its author's, whitespace
    made single spaces, cut to the standard's limit), its `folder` as found
    (a link kept as one), the kind of skills folder it was found in
    (`found_in`, one of FOUND_IN) and that folder (`skills_folder`)."""

    name: str
    description: str
    folder: Path
    found_in: str
    skills_folder: Path

    @property
    def path(self):
        return self.folder / SKILL_FILE


@dataclass
class Found:
    """What a look found: the skills a session can use, nearest folder first
    and by name within one, and the notes for the user, a line each."""

    skills: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def named(self, name):
        """The skill by this name, or None."""
        return next((s for s in self.skills if s.name == name), None)


class SkillProblem(Exception):
    """A SKILL.md that can't be used, and why."""


def one_line(text):
    """Text with every run of whitespace a single space."""
    return " ".join(str(text).split())


def cut(text, chars):
    """`text` in `chars` characters at most: cut after a whole word where one
    ends in its second half, and an ellipsis."""
    if len(text) <= chars:
        return text
    kept = text[:chars - 1]
    word_end = kept.rfind(" ")
    if word_end > chars // 2:
        kept = kept[:word_end]
    return kept.rstrip(" ,;:") + "…"


def quoted(block):
    """The frontmatter with each plain one-line value that holds ": " in
    double quotes: invalid YAML other readers take (Codex's repair)."""
    lines = []
    for line in block.splitlines():
        found = PLAIN_FIELD.match(line)
        if found and ": " in found.group(2):
            value = found.group(2).replace("\\", "\\\\").replace('"', '\\"')
            line = f'{found.group(1)}: "{value}"'
        lines.append(line)
    return "\n".join(lines)


def parsed(text):
    """(fields, instructions) of a SKILL.md's text: its frontmatter's fields
    and what follows it, trimmed. Raises SkillProblem."""
    found = FRONTMATTER.match(text.removeprefix("﻿"))
    if found is None:
        raise SkillProblem("no frontmatter between --- lines at its start")
    block, instructions = found.group(1), text[found.end():].strip()
    problem = None
    for attempt in (block, quoted(block)):
        try:
            fields = yaml.safe_load(attempt)
        except yaml.YAMLError as error:
            problem = one_line(error)
            continue
        if not isinstance(fields, dict):
            raise SkillProblem("its frontmatter holds no fields")
        return fields, instructions
    raise SkillProblem(f"its frontmatter isn't YAML ({problem})")


def read_file(path):
    """A SKILL.md's text. Raises SkillProblem."""
    try:
        if path.stat().st_size > MOST_FILE_BYTES:
            raise SkillProblem(f"past {MOST_FILE_BYTES // 1024} KB")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise SkillProblem(f"unreadable ({one_line(error)})") from error


def instructions_of(skill):
    """(instructions, compatibility) of a skill, read now: what its SKILL.md
    holds after its frontmatter, and what its author says it needs (the
    standard's `compatibility`), or None. Raises SkillProblem."""
    fields, instructions = parsed(read_file(skill.path))
    needs = fields.get("compatibility")
    needs = cut(one_line(needs), COMPATIBILITY_CHARS) if needs else None
    return instructions, needs


def skill_in(folder, found_in, skills_folder, notes):
    """The skill in `folder`, or None (no SKILL.md, or one that can't be
    used: noted)."""
    path = folder / SKILL_FILE
    if not path.is_file():
        return None
    try:
        fields, _ = parsed(read_file(path))
    except SkillProblem as problem:
        notes.append(f"skill left out, {path}: {problem}")
        return None
    name = one_line(fields.get("name") or folder.name)
    description = fields.get("description")
    if not isinstance(description, str) or not description.strip():
        notes.append(f"skill left out, {path}: no description")
        return None
    if fields.get("disable-model-invocation") is True:
        notes.append(
            f"skill {name} ({path}) not listed: its author keeps it for the "
            "user to start (disable-model-invocation), and ZERO starts none "
            "for the user")
        return None
    if not NAME_FORM.match(name) or len(name) > NAME_CHARS:
        notes.append(f"skill {name} ({path}): its name isn't in the standard's "
                     "form (lowercase letters, digits, single hyphens; 64 at "
                     "most)")
    elif name != folder.name:
        notes.append(f"skill {name} ({path}): its name isn't its folder's")
    description = one_line(description)
    if len(description) > DESCRIPTION_CHARS:
        notes.append(
            f"skill {name} ({path}): its description is cut to the "
            f"standard's {DESCRIPTION_CHARS:,} characters, from "
            f"{len(description):,}")
        description = cut(description, DESCRIPTION_CHARS)
    return Skill(name, description, folder, found_in, skills_folder)


def skills_in(skills_folder, found_in, notes):
    """The skills one level down in `skills_folder`, by folder name; hidden
    folders passed over."""
    try:
        entries = sorted(os.scandir(skills_folder), key=lambda e: e.name)
    except OSError:
        return []
    found = []
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        skill = skill_in(Path(entry.path), found_in, skills_folder, notes)
        if skill is not None:
            found.append(skill)
    return found


def files_of(folder):
    """([the files in a skill's folder but its SKILL.md, relative to it],
    how many more there are): hidden ones, caches and installed packages
    passed over, links in it not followed, FILES_LISTED at most."""
    listed, more, looked = [], 0, 0
    for top, folders, files in os.walk(folder):
        folders[:] = sorted(f for f in folders
                            if not f.startswith(".") and f not in NOT_THE_SKILLS)
        for name in sorted(files):
            looked += 1
            if name.startswith("."):
                continue
            relative = (Path(top) / name).relative_to(folder)
            if relative == Path(SKILL_FILE):
                continue
            if len(listed) < FILES_LISTED:
                listed.append(relative.as_posix())
            else:
                more += 1
        if looked >= FILES_LOOKED_AT:
            break
    return listed, more


class SkillFolders:
    """The door a session finds skills through, which the assembly hands it
    (design rule 3): the skills folders for its `workspace`, nearest first,
    read as they stand at each look (`found`). `own`: ZERO's own skills
    folder, or None; `shared`: the user's, the agents' shared one (by
    default paths.shared_skills_folder)."""

    def __init__(self, workspace, own=None, shared=None):
        self.workspace = Path(workspace)
        self.own = Path(own) if own is not None else None
        self.shared = Path(shared) if shared is not None else (
            paths.shared_skills_folder())

    def folders(self):
        """[(found_in, skills folder)], nearest first, each once: a project's
        folder that is the user's own (a workspace of the home) is theirs."""
        chosen = []
        root = project_root(self.workspace)
        if root is not None:
            chosen += [("project", folder / STANDARD_FOLDER)
                       for folder in reversed(walk(root, self.workspace))]
        if self.own is not None:
            chosen.append(("zero", self.own))
        chosen.append(("user", self.shared))
        seen, kept = {}, []
        for found_in, folder in reversed(chosen):
            real = os.path.realpath(folder)
            if real not in seen:
                seen[real] = True
                kept.append((found_in, folder))
        return list(reversed(kept))

    def found(self):
        """The skills in reach now, and the notes for the user (Found)."""
        found = Found()
        nearer = {}
        for found_in, folder in self.folders():
            for skill in skills_in(folder, found_in, found.notes):
                if skill.name in nearer:
                    found.notes.append(
                        f"skill {skill.name} ({skill.path}) not used: the one "
                        f"in {nearer[skill.name]} is nearer")
                    continue
                nearer[skill.name] = folder
                found.skills.append(skill)
        return found
