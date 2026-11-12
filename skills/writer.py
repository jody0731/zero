"""A skill ZERO writes, into its own skills folder in its home (design rule
10: whole or not at all).

A skill is a folder: its SKILL.md and the files beside it. A write builds the
whole folder in a hidden one beside it (the old skill's files copied in, the
named ones written or removed, SKILL.md last), then moves it into place in one
step; a skill written over is first moved aside, under a hidden name, and
removed once the new one is in. A crash between those two moves leaves the old
skill aside with no skill in its place: the next write puts it back. Hidden
folders are never read as skills (found.py), so a reader sees the old skill or
the new one, never half of one.

What is written is held to the standard where a skill found elsewhere is only
noted (found.py reads leniently): a name in its form, which is its folder's; a
description, 1,024 characters at most; instructions; SKILL.md no larger than
found.py reads. A name another skills folder already uses is refused: ZERO's
own would come before the user's shared one, or after the project's, and
neither is what the writer meant.
"""

import os
import secrets
import shutil
from pathlib import Path, PurePosixPath

import yaml

import paths
from skills.found import (
    DESCRIPTION_CHARS, FILES_LISTED, MOST_FILE_BYTES, NAME_CHARS, NAME_FORM,
    SKILL_FILE, SkillProblem, parsed, read_file,
)

# The frontmatter fields a write sets; a skill written over keeps its others
# (license, metadata, allowed-tools ...)
WRITTEN_FIELDS = ("name", "description", "compatibility")
# The most a write's files hold together, SKILL.md aside
MOST_FILES_BYTES = 4 * MOST_FILE_BYTES
# Hidden names a write uses beside the skill's folder
BUILDING = ".{name}.writing-"
ASIDE = ".{name}.replaced-"


class NotWritten(Exception):
    """Why a skill wasn't written, in the words its caller reads."""


def checked_name(name):
    name = str(name or "").strip()
    if not NAME_FORM.match(name) or len(name) > NAME_CHARS:
        raise NotWritten(
            f"the name {name!r} isn't in the standard's form: lowercase "
            f"letters, digits and single hyphens inside, {NAME_CHARS} at most")
    return name


def checked_files(files):
    """{relative path: text or None (remove it)}: each path inside the
    skill's folder, not SKILL.md."""
    if files is None:
        return {}
    if not isinstance(files, dict):
        raise NotWritten("files is an object: a path in the skill's folder "
                         "for each file, its text the value (null removes it)")
    if len(files) > FILES_LISTED:
        raise NotWritten(f"a write names {FILES_LISTED} files at most")
    checked, size = {}, 0
    for path, text in files.items():
        relative = PurePosixPath(str(path))
        if (relative.is_absolute() or not relative.parts
                or ".." in relative.parts or str(relative) == "."):
            raise NotWritten(f"{path!r} isn't a path inside the skill's folder")
        if relative == PurePosixPath(SKILL_FILE):
            raise NotWritten(f"{SKILL_FILE} is written from description and "
                             "instructions, not from files")
        if text is not None and not isinstance(text, str):
            raise NotWritten(f"the text of {path} is a string (null removes it)")
        size += len((text or "").encode())
        checked[relative.as_posix()] = text
    if size > MOST_FILES_BYTES:
        raise NotWritten(f"the files hold {size:,} bytes, past the "
                         f"{MOST_FILES_BYTES:,} a write takes")
    return checked


def skill_text(name, description, instructions, compatibility=None,
               kept_fields=None):
    """A SKILL.md: frontmatter (YAML, so any text in a value stays one
    value), then the instructions."""
    description = " ".join(str(description or "").split())
    if not description:
        raise NotWritten("description is empty")
    if len(description) > DESCRIPTION_CHARS:
        raise NotWritten(f"description is {len(description):,} characters, "
                         f"past the standard's {DESCRIPTION_CHARS:,}")
    instructions = str(instructions or "").strip()
    if not instructions:
        raise NotWritten("instructions is empty")
    fields = {k: v for k, v in (kept_fields or {}).items()
              if k not in WRITTEN_FIELDS}
    fields = {"name": name, "description": description, **fields}
    if compatibility:
        fields["compatibility"] = " ".join(str(compatibility).split())
    front = yaml.safe_dump(fields, sort_keys=False, allow_unicode=True,
                           width=1_000_000)
    text = f"---\n{front}---\n\n{instructions}\n"
    if len(text.encode()) > MOST_FILE_BYTES:
        raise NotWritten(f"{SKILL_FILE} would be past the "
                         f"{MOST_FILE_BYTES // 1024} KB ZERO reads")
    return text


def put_back(own, name):
    """What a write cut short left: a skill moved aside with none in its
    place goes back; anything else hidden of this name's is removed."""
    folder = own / name
    try:
        entries = list(os.scandir(own))
    except OSError:
        return
    for entry in entries:
        aside = entry.name.startswith(ASIDE.format(name=name))
        building = entry.name.startswith(BUILDING.format(name=name))
        if not (aside or building):
            continue
        if aside and not os.path.lexists(folder):
            os.rename(entry.path, folder)
        else:
            shutil.rmtree(entry.path, ignore_errors=True)


def write_file(path, text):
    """One file of the skill, whole; a script (a #! line) runnable."""
    mode = 0o755 if text.startswith("#!") else 0o644
    paths.write_whole(path, text, mode=mode)


def write(own, name, description, instructions, files=None,
          compatibility=None):
    """Write skill `name` into ZERO's own skills folder `own`: (its folder,
    whether a skill was written over). Raises NotWritten."""
    name = checked_name(name)
    files = checked_files(files)
    own = Path(own)
    folder = own / name
    if folder.is_symlink():
        raise NotWritten(f"{folder} is a link to {os.path.realpath(folder)}, "
                         "a skill kept there; it is changed where it is kept")
    own.mkdir(mode=0o700, parents=True, exist_ok=True)
    put_back(own, name)
    replaced = folder.is_dir()
    kept_fields = {}
    if replaced:
        try:
            kept_fields, _ = parsed(read_file(folder / SKILL_FILE))
        except SkillProblem:
            kept_fields = {}
    text = skill_text(name, description, instructions, compatibility,
                      kept_fields)
    building = Path(os.path.join(
        own, BUILDING.format(name=name) + secrets.token_hex(4)))
    try:
        if replaced:
            shutil.copytree(folder, building, symlinks=True)
        else:
            building.mkdir(mode=0o755)
        for relative, content in files.items():
            target = building / relative
            if content is None:
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            # A link copied from the old skill is replaced, never written
            # through to wherever it points
            if target.is_symlink():
                target.unlink()
            write_file(target, content)
        write_file(building / SKILL_FILE, text)
        if replaced:
            aside = own / (ASIDE.format(name=name) + secrets.token_hex(4))
            os.rename(folder, aside)
            os.rename(building, folder)
            shutil.rmtree(aside, ignore_errors=True)
        else:
            os.rename(building, folder)
    except BaseException:
        shutil.rmtree(building, ignore_errors=True)
        raise
    return folder, replaced
