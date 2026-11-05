"""`memory`: add, replace or remove text in one of ZERO's own files.

The harness writes the file itself, in no sandbox: the call is what is asked
about. Its crossing asks in every mode, so the mode's approver answers it (the
user; the judge in the auto modes; the user again in bypassPermissions, which
asks nothing else; nobody in readOnly): a remembered line reaches every later
session, so a planted one would too (Hermes scans entries for injected text;
here someone answers for each). A change is live at once: its result holds
the file as sessions read it, so the session that made it has it from there
on, and later sessions read it at their start. The system message stays as
the session started: an approved change, never a file changed on disk, is
what a session takes in mid-way. Which file a target is comes from the
session (`own_files`), never from the model.
"""

import os
import re
import tempfile
from pathlib import Path

from core.tools import tool
from prompt.instructions import CAPS, clean

TARGETS = ("soul", "user", "project")
ACTIONS = ("add", "replace", "remove")
# Each target as an ask and the judge name it, and as its own refusals do
NAMES = {
    "soul": "SOUL.md", "user": "the user's ZERO.md",
    "project": "the project's ZERO.md",
}
FILES = {"soul": "SOUL.md", "user": "ZERO.md", "project": "the project's ZERO.md"}
CROSSES = "It changes what every later session reads."
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$")
# How much of a change an ask and the judge see
SHOWN = 2000
# Where a change lives from now: this session (its result) and every later
# one (the file)
NOW = (
    "It now reads as below, in this session from here on, where the system "
    "message still shows it as the session started, and in every session "
    "started from now."
)


class Refusal(str):
    """Why a change isn't made, with {name} for the file's name."""


def title(heading):
    """A heading's words, without its #s."""
    return str(heading).strip().lstrip("#").strip()


def changed(text, action, new, old, heading):
    """`text` after the change, or a Refusal."""
    if action == "add":
        if not new or not new.strip():
            return Refusal("Not changed: add needs text.")
        return added(text, new.strip("\n"), heading)
    if action not in ACTIONS:
        return Refusal("Not changed: action is add, replace or remove.")
    if action == "replace" and (new is None or not old):
        return Refusal("Not changed: replace needs text and old_text.")
    if action == "remove" and not old:
        return Refusal("Not changed: remove needs old_text.")
    count = text.count(old)
    if count == 0:
        return Refusal("Not changed: old_text isn't in {name}.")
    if count > 1:
        return Refusal(
            f"Not changed: old_text occurs {count} times in {{name}}; it names "
            "one place."
        )
    if action == "replace":
        return text.replace(old, new, 1)
    at = text.index(old)
    end = at + len(old)
    # A whole line goes with its line ending, so no empty line is left
    if (at == 0 or text[at - 1] == "\n") and text[end:end + 1] == "\n":
        end += 1
    return text[:at] + text[end:]


def added(text, new, heading):
    """`new` at the end of `heading`'s section, the heading made at the end
    when missing; at the end of the file with no heading."""
    body = text.rstrip("\n")
    lead = body + "\n\n" if body else ""
    if not heading or not title(heading):
        return f"{lead}{new}\n"
    wanted = title(heading).lower()
    lines = text.split("\n")
    found = None
    for number, line in enumerate(lines):
        match = HEADING.match(line)
        if match and match.group(1).lower() == wanted:
            found = number
            break
    if found is None:
        return f"{lead}# {title(heading)}\n\n{new}\n"
    end = next(
        (n for n in range(found + 1, len(lines)) if HEADING.match(lines[n])),
        len(lines),
    )
    at = end
    while at > found + 1 and not lines[at - 1].strip():
        at -= 1
    # An empty section gets a blank line under its heading first
    inserted = ([""] if at == found + 1 else []) + new.split("\n")
    done = "\n".join(lines[:at] + inserted + lines[at:])
    return done if done.endswith("\n") else done + "\n"


def store(path, text):
    """Write the whole file at once, through any link to it, keeping its
    permissions."""
    real = Path(os.path.realpath(path))
    real.parent.mkdir(parents=True, exist_ok=True)
    mode = real.stat().st_mode & 0o777 if real.exists() else None
    handle, temporary = tempfile.mkstemp(dir=real.parent, prefix=".memory-")
    try:
        with os.fdopen(handle, "w") as out:
            out.write(text)
        if mode is None:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.chmod(temporary, mode)
        os.replace(temporary, real)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def clipped(text):
    text = str(text or "")
    if len(text) <= SHOWN:
        return text
    return f"{text[:SHOWN]}… [{len(text) - SHOWN:,} more characters]"


def summary(args):
    """The change, as an ask and the judge read it."""
    name = NAMES.get(args.get("target"), str(args.get("target")))
    action = args.get("action")
    if action == "add":
        under = f" under “{title(args['heading'])}”" if args.get("heading") else ""
        return f"memory: add to {name}{under}:\n{clipped(args.get('text'))}"
    if action == "replace":
        return (
            f"memory: replace in {name}:\n{clipped(args.get('old_text'))}\nwith\n"
            f"{clipped(args.get('text'))}"
        )
    return f"memory: {action} from {name}:\n{clipped(args.get('old_text'))}"


def crosses(args):
    return {"rule": "memory", "fact": CROSSES, "why": args.get("reason"),
            "every_mode": True}


DESCRIPTION = (
    "Change your own files, which every session reads at its start: SOUL.md "
    "(target soul: your character), the user's ZERO.md (target user: the user, "
    "their machines, their instructions and what you remember) and the "
    "project's ZERO.md (target project: at the git root, else the workspace). "
    "add puts text at the end of a heading's section (the heading is made when "
    "missing; with none, at the end of the file); replace swaps old_text for "
    "text; remove deletes old_text. old_text occurs exactly once. Each change "
    "goes first to the session's approver, whom the environment's mode line "
    "names. A change's result holds the file as it then reads, for this "
    "session; later sessions read it at their start. SOUL.md holds "
    f"{CAPS['soul']:,} characters, ZERO.md {CAPS['user']:,}, the project's "
    f"ZERO.md {CAPS['project']:,}; a change past that isn't made. The file "
    "tools don't change these files; the user edits them by hand too."
)


@tool(
    DESCRIPTION,
    optional=("text", "old_text", "heading", "reason"),
    summary=summary,
    ready=lambda context: context.get("own_files") is not None,
    crosses=crosses,
    target=({"type": "string", "enum": list(TARGETS)}, "Which file"),
    action=({"type": "string", "enum": list(ACTIONS)}, "What to do"),
    text=("string", "For add, the text; for replace, what takes old_text's place"),
    old_text=("string", "For replace and remove, the exact text, found once"),
    heading=("string", "For add, the heading whose section it goes under"),
    reason=("string", "Why the change"),
)
def memory(target, action, text=None, old_text=None, heading=None, reason="",
           own_files=None):
    """`own_files`: the session's {target: path}; the project's is None for a
    workspace of /."""
    if target not in TARGETS:
        return "Not changed: target is soul, user or project."
    path = (own_files or {}).get(target)
    if path is None:
        return (
            "Not changed: this session's workspace is the whole machine (/), "
            "which has no project ZERO.md."
        )
    path = Path(path)
    try:
        before = path.read_text()
    except FileNotFoundError:
        before = ""
    after = changed(before, action, text, old_text, heading)
    if isinstance(after, Refusal):
        return after.format(name=path)
    size = len(clean(after))
    cap = CAPS[target]
    if size > cap:
        return (
            f"Not changed: {FILES[target]} would hold {size:,} characters as "
            f"sessions read it; it holds {cap:,}. Removing or shortening "
            "entries makes room."
        )
    store(path, after)
    if action == "add":
        under = f" under “{title(heading)}”" if heading and title(heading) else ""
        done = f"Added to {path}{under}."
    elif action == "replace":
        done = f"Replaced in {path}."
    else:
        done = f"Removed from {path}."
    return f"{done} {NOW}\n\n{clean(after)}"
