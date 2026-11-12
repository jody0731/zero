"""The skills found, in the system message: what a skill is, then each skill
under the folder it was found in, with its author's description; read_skill
reads one in full. Listed when the system message is written (a session's
first turn, a compaction, a switch, new code), as the user's files are read
then: a skill installed since is listed at the next, and read_skill finds it
now. The list takes LIST_PERCENT of the session's line at most (Codex's 2% of
the window): past it, every description is cut alike, to
LEAST_DESCRIPTION_CHARS at the least, and the skills that still don't fit are
named, their descriptions left out.
"""

from core.hooks import Hook
from skills.found import cut

# The share of the session's line the list takes at most, and the least of
# a description it cuts to before it names a skill alone
LIST_PERCENT = 2
LEAST_DESCRIPTION_CHARS = 100
# The tool that reads a skill in full (tool.py)
READER = "read_skill"
# What a skill is, said once above the list; the descriptions are their
# authors' words, which the list marks as theirs
OPENING = (
    "\n\nSkills, as found when this system message was written: each a folder "
    "of instructions for a kind of task, often with scripts or other files "
    "beside them, made for any agent that reads the Agent Skills standard. "
    "Each description is its skill's author's words; {reader} reads a skill's "
    "instructions in full, with its folder and files."
)
NONE_FOUND = (
    "\n\nSkills: none is installed. A skill is a folder of instructions for a "
    "kind of task by the Agent Skills standard, read from {folders}."
)
# The heading of each skills folder's part of the list, by the kind of
# folder it is
FOLDER_HEADINGS = {
    "project": "In the project's skills folder, {folder}:",
    "zero": "In your own skills folder, {folder}:",
    "user": ("In the user's skills folder, {folder}, which the agents on this "
             "machine share:"),
}
# Where skills are read from, in NONE_FOUND
FOLDER_NAMES = {
    "project": "the project's {folder}",
    "zero": "your own {folder}",
    "user": "the user's {folder}",
}
# The skills past the list's room, by name
NAMED_ONLY = "Also found, their descriptions left out for room: {names}."


def line(skill, chars=None):
    """A skill's line in the list, its description cut to `chars` (None:
    whole)."""
    text = skill.description if chars is None else cut(skill.description, chars)
    return f"- {skill.name}: {text}"


def fitted(skills, room):
    """([each listed skill's line], [the names left past the room]): every
    description whole where the lines fit in `room` characters; else each
    cut to the longest length that fits, LEAST_DESCRIPTION_CHARS at the
    least; past that, the skills in order while they fit, the rest named."""

    def size(lines):
        return sum(len(text) + 1 for text in lines)

    whole = [line(s) for s in skills]
    if size(whole) <= room:
        return whole, []
    longest = max(len(s.description) for s in skills)
    low, high = LEAST_DESCRIPTION_CHARS, longest
    best = None
    while low <= high:
        middle = (low + high) // 2
        if size(line(s, middle) for s in skills) <= room:
            best, low = middle, middle + 1
        else:
            high = middle - 1
    if best is not None:
        return [line(s, best) for s in skills], []
    lines, used = [], 0
    for at, skill in enumerate(skills):
        text = line(skill, LEAST_DESCRIPTION_CHARS)
        if used + len(text) + 1 > room:
            return lines, [s.name for s in skills[at:]]
        lines.append(text)
        used += len(text) + 1
    return lines, []


def skill_list(found, room, folders):
    """The system message's part for the skills `found` (a list of Skill),
    in `room` characters for their lines; `folders`, the session's skills
    folders as SkillFolders.folders gives them, named when none is found."""
    if not found:
        named = [FOLDER_NAMES[kind].format(folder=folder)
                 for kind, folder in folders]
        return NONE_FOUND.format(folders=", ".join(named))
    lines, past = fitted(found, room)
    by_skill = dict(zip((s.name for s in found), lines))
    parts = [OPENING.format(reader=READER)]
    heading = None
    for skill in found:
        if skill.name not in by_skill:
            break
        if skill.skills_folder != heading:
            heading = skill.skills_folder
            parts.append(FOLDER_HEADINGS[skill.found_in].format(folder=heading))
        parts.append(by_skill[skill.name])
    if past:
        parts.append(NAMED_ONLY.format(names=", ".join(past)))
    return "\n".join(parts)


class Skills(Hook):
    """The skills a session finds through its door (`folders`, a
    SkillFolders), listed in its system message. `line_chars()`: the
    session's line in characters, as it stands, whose LIST_PERCENT the list
    takes at most."""

    def __init__(self, folders, line_chars):
        self.folders = folders
        self.line_chars = line_chars

    def system_prompt(self, session):
        room = self.line_chars() * LIST_PERCENT // 100
        return skill_list(self.folders.found().skills, room,
                          self.folders.folders())
