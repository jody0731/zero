"""Changes to instruction files need approval of the exact edit from the user.

The question tool shows a proposal bound to the file's current contents.
Its recorded approval is used once by this tool, independently of the mode's
judge. The file state is rechecked while holding the writer lock.
"""

import hashlib
import os
import re
from pathlib import Path

import paths
from core import tools
from core.tools import tool
from prompt.instructions import clean

TARGETS = ("soul", "user", "project")
ACTIONS = ("add", "replace", "remove")
# Each target as an ask and the judge name it, and as its own refusals do
ASK_NAMES = {
    "soul": "SOUL.md", "user": "the user's ZERO.md",
    "project": "the project's ZERO.md",
}
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$")
# Compact summaries may clip; the user's proposal always shows the whole edit.
SHOWN_CHARS = 2000
# Where a change lives from now: this session (its result) and every later
# one (the file)
NOW = (
    "It now reads as below, in this session from here on, where the system "
    "message still shows it as it read when that message was written, and in "
    "every session started from now."
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
    """Write the whole file at once (paths.write_whole), through any link to
    it, keeping its permissions."""
    paths.write_whole(Path(os.path.realpath(path)), text)


def clipped(text, whole=False):
    text = str(text or "")
    if whole or len(text) <= SHOWN_CHARS:
        return text
    return f"{text[:SHOWN_CHARS]}… [{len(text) - SHOWN_CHARS:,} more characters]"


def summary(args, whole=False):
    """The change, as an ask shows it and the judge reads it (`whole`)."""
    name = ASK_NAMES.get(args.get("target"), str(args.get("target")))
    action = args.get("action")
    text = clipped(args.get("text"), whole)
    old = clipped(args.get("old_text"), whole)
    if action == "add":
        under = f" under “{title(args['heading'])}”" if args.get("heading") else ""
        return f"memory: add to {name}{under}:\n{text}"
    if action == "replace":
        return f"memory: replace in {name}:\n{old}\nwith\n{text}"
    return f"memory: {action} from {name}:\n{old}"


def proposal(args, context):
    path = (context.get("own_files") or {}).get(args.get("target"))
    if path is None:
        raise ValueError("This instruction file is unavailable in this session.")
    path = Path(os.path.realpath(path))
    try:
        before = path.read_text()
        existed = True
    except FileNotFoundError:
        before, existed = "", False
    after = changed(before, args.get("action"), args.get("text"),
                    args.get("old_text"), args.get("heading"))
    if isinstance(after, Refusal):
        raise ValueError(after.format(name=path))
    digest = lambda text: hashlib.sha256(text.encode()).hexdigest()
    return {
        "text": f"File: {path}\n{summary(args, whole=True)}",
        "state": {"path": str(path), "existed": existed,
                  "before": digest(before), "after": digest(after)},
    }


def describe(limits, context=None):
    """The tool's description, with the share of the context the three files
    take as it stands (`own_files_percent`, the assembly's); without a
    session, the share isn't a number yet."""
    percent = (context or {}).get("own_files_percent")
    share = f"{percent()}%" if percent is not None else "a share"
    question = tools.question_tool() or "the question tool"
    return (
        "Change your own instruction files: SOUL.md "
        "(target soul: your character), the user's ZERO.md (target user: the "
        "user, their machines, their instructions and what you remember) and "
        "the project's ZERO.md (target project: at the git root, else the "
        "workspace). Each fact belongs in one file. Memory entries are dated "
        "bullets. Pruning stale entries keeps room for current ones. "
        "add puts text at the end of a heading's section (the "
        "heading is made when missing). Without a heading, add appends to the "
        "file. Replace swaps old_text for text. Remove deletes old_text. "
        "Every edit needs the user's go on the exact change. "
        f"Use {question} with proposed_call containing this tool and its "
        "complete arguments, and explain why. After approval, make that "
        "call. Changed file contents or a different edit need fresh approval. "
        "An approved edit makes the updated instructions live in this session. "
        f"New sessions read these files in {share} of the context limit, with a "
        "note if a file is cut."
    )


@tool(
    describe,
    optional=("text", "old_text", "heading", "reason"),
    summary=summary,
    ready=lambda context: context.get("own_files") is not None,
    needs_permission=lambda args: False,
    user_approval=proposal,
    target=({"type": "string", "enum": list(TARGETS)}, "Which file"),
    action=({"type": "string", "enum": list(ACTIONS)}, "What to do"),
    text=("string", "For add, the text; for replace, what takes old_text's place"),
    old_text=("string", "For replace and remove, the exact text, found once"),
    heading=("string", "For add, the heading whose section it goes under"),
    reason=("string", "Why the change"),
)
def memory(target, action, text=None, old_text=None, heading=None, reason="",
           own_files=None, notes=None):
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
    path = Path(os.path.realpath(path))
    args = {"target": target, "action": action, "text": text,
            "old_text": old_text, "heading": heading, "reason": reason}
    approved = (notes or {}).get("user_approval")
    if not approved:
        return tools.Result("Not changed: the user has not approved this edit.",
                            failed=True)
    with paths.changing(path):
        current = proposal(args, {"own_files": own_files})
        if current["state"] != approved["state"]:
            return tools.Result(
                "Not changed: the file or edit changed after approval.",
                failed=True,
            )
        try:
            before = path.read_text()
        except FileNotFoundError:
            before = ""
        after = changed(before, action, text, old_text, heading)
        if isinstance(after, Refusal):
            return tools.Result(after.format(name=path), failed=True)
        store(path, after)
    if action == "add":
        under = f" under “{title(heading)}”" if heading and title(heading) else ""
        done = f"Added to {path}{under}."
    elif action == "replace":
        done = f"Replaced in {path}."
    else:
        done = f"Removed from {path}."
    size = f"It holds {len(clean(after)):,} characters as sessions read it."
    return f"{done} {size} {NOW}\n\n{clean(after)}"


# What this component offers the model, in the order it reads them; the
# assembly adds them to core's registry (core/tools.py, add)
TOOLS = (memory,)
