"""What a session reads at its start, after the code's own lines (what ZERO
is and runs on, when it acts): ZERO's character (SOUL.md),
the user's ZERO.md (their instructions, them, their machines and what ZERO
remembers) and the project's ZERO.md files, from the git root down to the
workspace (Codex's walk, with ZERO's own file name).

The user edits every one of them by hand; ZERO changes them only through its
memory tool (memory/), so the harness's own rules (how its tools work, what
they may touch) stay in code, where no file can undo them. Another agent's
files on the walk (AGENTS.md, CLAUDE.md) are found and named, never read in:
they are written for those agents. The files share one room, a share of the
session's context that the assembly works out, and a cut says where the rest
is (OpenClaw, Hermes). HTML comments are for the user and never reach
the model; a heading left with nothing under it is dropped too. The first use
writes the user's two files from the templates beside this file.

HARNESS is code, not a file: SOUL.md says what ZERO is like and may rename
it, but what it runs on is the session's to say (the model it was made
with), the line Codex and Hermes open with too. Without it, SOUL.md is all
ZERO knows of itself, and a model reading CLAUDE.md can take ZERO for a
persona worn by Claude Code. INTENT is code for the same reason: when ZERO
acts holds for every user, so no file has to say it. Its certainty rests on
what can be checked (the user's words, what looking shows), not on a feeling,
since a model is most often wrong when it feels sure. It is the agent's
alone: the judge asks only whether a call serves the goal (judge/verdict.py).
A user's ZERO.md, read after it, can refine it.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import paths
from core import history

HERE = Path(__file__).resolve().parent
SOUL = HERE / "SOUL.md"
ZERO = HERE / "ZERO.md"
# The file a project's folders hold for ZERO, and other agents' own
PROJECT_FILE = "ZERO.md"
OTHER_AGENT_FILES = ("AGENTS.md", "CLAUDE.md")
# The code's own lines, the same for every user. What ZERO is and runs on,
# before anything a file says
HARNESS = (
    "You are Z.E.R.O. (a.k.a. Zealous Efferent Response Operator), an agent "
    "running on this machine in its own harness, with the model {model}."
)
# When to act: sure of the user's wish, from their words, and of the world,
# from looking; a gap is filled before acting, not guessed over
INTENT = (
    "You act when you are sure: sure the user asked for the action, from their "
    "own words, and sure of what you act on, from looking. Where you aren't, "
    "you fill the gap before acting: the facts by looking, the user's wish by "
    "asking."
)
# How a turn ends: the loop stops at a reply with no tool call. The rest the
# model learns from facts: the environment, the tools' descriptions, what
# each call returns
TURN_RULE = (
    "\nWhen you have the answer, reply in plain text without calling a tool.\n"
)
# What a notice is, said once: each one carries only its tag
# (core/history.py), and each part that sends them names itself. The reply's
# fact: without it the model hesitated over ending a notice's turn, as if
# that left the user without an answer
NOTICE_FACT = (
    'A message in a <notice from="…"> tag is a notice from ZERO, not a message '
    "from the user: {senders}. The reply to a notice reaches the user as any "
    "reply does."
)
# The longest piece of a line a cut quotes
QUOTED_CHARS = 60
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
HEADING = re.compile(r"^#{1,6} ")


@dataclass(frozen=True)
class Instructions:
    """What a session reads: `soul` and `user`, the paths of SOUL.md and the
    user's ZERO.md (None: none); `project`, whether the project's ZERO.md
    files are read; `memory`, whether the memory tool may change them."""

    soul: Path | None = None
    user: Path | None = None
    project: bool = False
    memory: bool = False


@dataclass
class InstructionsRead:
    """What a session read at its start: the text that leads its system
    prompt, the files it came from, where the project's ZERO.md is (None for
    a workspace of /, which has no project) and the other agents' files the
    walk found."""

    text: str = ""
    soul: Path | None = None
    user: Path | None = None
    project: list = field(default_factory=list)
    project_file: Path | None = None
    others: list = field(default_factory=list)

    def record(self):
        """What the session's log keeps of it."""
        return {
            "soul": str(self.soul) if self.soul else None,
            "zero_md": str(self.user) if self.user else None,
            "project": [str(p) for p in self.project],
        }


def ensure(path, template):
    """The user's file, written from `template` when there is none."""
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template.read_text())
    return path


def text(path):
    """The file as the model gets it (`clean`); "" for no file."""
    try:
        return clean(Path(path).read_text())
    except (FileNotFoundError, IsADirectoryError):
        return ""


def clean(raw):
    """A file's text as the model gets it: comments out, empty sections out,
    blank runs squeezed."""
    sections = []
    for part in re.split(r"(?m)^(?=#{1,6} )", COMMENT.sub("", raw)):
        part = part.strip()
        if not part:
            continue
        head, _, body = part.partition("\n")
        if HEADING.match(head) and not body.strip():
            continue
        sections.append(re.sub(r"\n{3,}", "\n\n", part))
    return "\n\n".join(sections)


def cut(body, room, path, limit):
    """`body` in `room` characters at most: whole lines, then a line that says
    where the rest of `path` goes on. `limit` is the room the files share,
    which the note names."""
    if len(body) <= room:
        return body
    kept = []
    for line in body.split("\n"):
        quoted = line.strip()[:QUOTED_CHARS]
        note = (
            f"[Cut at {limit:,} characters, what these files take together: "
            f"{path} goes on from the line “{quoted}”.]"
        )
        # The line that doesn't fit beside its note is where the rest starts
        if len("\n".join(kept + [line])) + 2 + len(note) > room:
            return ("\n".join(kept) + "\n\n" + note).lstrip("\n")
        kept.append(line)
    return body


def project_root(workspace):
    """The project a workspace is in: the nearest folder up from it with a
    .git, else the workspace itself; None for a workspace of /."""
    workspace = Path(workspace).resolve()
    if workspace == Path("/"):
        return None
    for folder in (workspace, *workspace.parents):
        if (folder / ".git").exists():
            return folder
    return workspace


def walk(root, workspace):
    """The folders from the project's root down to the workspace."""
    workspace = Path(workspace).resolve()
    folders = [workspace]
    while folders[-1] != root and folders[-1] != folders[-1].parent:
        folders.append(folders[-1].parent)
    return list(reversed(folders))


def read(instructions, workspace, most_chars):
    """What a session with these instructions reads in `workspace`: its
    files in order, SOUL.md, the user's ZERO.md, the project's, in
    `most_chars` characters together."""
    found = InstructionsRead(soul=instructions.soul, user=instructions.user)
    parts = []
    room = most_chars

    def add(label, body, path):
        nonlocal room
        block = label + cut(body, room - len(label), path, most_chars)
        parts.append(block)
        room -= len(block) + 2

    for path in (instructions.soul, instructions.user):
        if path is not None and room > 0:
            body = text(path)
            if body:
                add("", body, path)
    root = project_root(workspace) if instructions.project else None
    if root is not None:
        found.project_file = root / PROJECT_FILE
        for folder in walk(root, workspace):
            found.others += [folder / n for n in OTHER_AGENT_FILES
                             if (folder / n).is_file()]
            path = folder / PROJECT_FILE
            body = text(path) if path.is_file() else ""
            if not body or room <= 0:
                continue
            add(f"The project's ZERO.md ({path}):\n", body, path)
            found.project.append(path)
    found.text = "\n\n".join(parts)
    return found


def notice_fact():
    """What a notice is, with each part that sends them."""
    senders = "; ".join(
        f'from="{source}", {what}' for source, what in history.NOTICES.items()
    )
    return NOTICE_FACT.format(senders=senders)


def system_prompt(found, model):
    """The system prompt's start: what ZERO is and runs on (`model`, the
    session's) and when it acts, what the session read
    (`found`, a Read; None for nothing), then how a turn ends and what a
    notice is; the hooks' parts follow."""
    opening = f"{HARNESS.format(model=model)}\n{INTENT}"
    read_in = found.text if found is not None else ""
    ending = f"{TURN_RULE}{notice_fact()}\n"
    if not read_in:
        return f"{opening}\n{ending}"
    return f"{opening}\n\n{read_in}\n{ending}"


def user_instructions():
    """The user's own files, written from the templates on first use, the
    project's, and the memory tool to change them: what a server's session
    reads."""
    return Instructions(
        soul=ensure(paths.soul_file(), SOUL),
        user=ensure(paths.zero_md_file(), ZERO),
        project=True,
        memory=True,
    )


def shipped_instructions():
    """The soul ZERO ships with and nothing else: the evals' yardstick, which
    the user's files never move."""
    return Instructions(soul=SOUL)
