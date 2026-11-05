"""The file tools: read with line ranges, write, exact-string edit.

Their disk work runs in helper.py through the session's own sandbox,
as bash's commands do, so the walls are the same ones (Codex runs apply_patch
this way): no second set of path checks. Where the mode has no sandbox, or a
call's ask to leave it was approved, the helper runs bare, as the user. Each
call notes the content's hash in its record (seen.py); no bytes are
kept: undo isn't the harness's. The stale check: `write` and `edit` change no
file that changed since this session's tools last read or wrote it, and
`write` replaces no existing file this session never read.
"""

import base64
import errno
import json
import os
import subprocess
import sys
from pathlib import Path

from core.tools import Grant, tool
from files import helper as filehelper
from sandbox.sandbox import NOT_OUTSIDE, WALL, leaves

# Lines a read returns at most, and where it starts cutting a long line
LINE_CAP = 2000
LONG_LINE = 2000
# Files bigger than this are left to commands
SIZE_CAP = 10 << 20
# A NUL byte in this much of a file's start makes it binary
BINARY_PROBE = 8192
HELPER_SECONDS = 30
HELPER = Path(filehelper.__file__).read_text()
# The system's own interpreter, not the venv's: the sandbox may hide the venv
# (the evals hide the whole repository); the helper needs only the stdlib
PYTHON = os.path.realpath(sys.executable)
WALL_ERRORS = (errno.EROFS, errno.EACCES, errno.EPERM)


class HelperError(Exception):
    """The helper couldn't run, or said nothing readable."""


def run_helper(box, request):
    """One request to the helper, through `box` (a Sandbox, Unsandboxed or
    Outside). The sandbox raises rather than hand back a bare command."""
    done = subprocess.run(
        box.command([PYTHON, "-I", "-c", HELPER]),
        input=json.dumps(request),
        capture_output=True,
        text=True,
        env=box.env(),
        cwd=box.workspace,
        timeout=HELPER_SECONDS,
    )
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError:
        tail = done.stderr.strip()[-300:] or f"exit code {done.returncode}"
        raise HelperError(f"the file helper failed: {tail}") from None


def pick_box(sandbox, outside, outside_sandbox):
    """The box a call runs in: `outside` when the call asks to leave the
    sandbox and may, else the session's own; a note when it couldn't leave."""
    box = sandbox
    if outside_sandbox is True:
        if outside is not None:
            return outside, ""
        if box.sandboxed:
            return box, NOT_OUTSIDE
    return box, ""


def real_path(box, path):
    """Where a path the helper saw is on the machine: a sandbox's /tmp is its
    private folder, kept only while the session is; None for those, which
    the restart note doesn't look at. The workspace is bound at its own path,
    even inside /tmp."""
    inside = Path(path)
    if not box.sandboxed or inside.is_relative_to(box.workspace):
        return path
    if inside.is_relative_to("/tmp"):
        return None
    return path


def failed(box, answer, verb, path):
    """A helper's OSError, as the model reads it; the sandbox's walls named
    when they are what stopped it."""
    text = f"Not {verb}: {path}: {answer['error']}."
    if box.sandboxed and answer.get("errno") in WALL_ERRORS:
        text += "\n\n" + WALL.format(walls=box.walls())
    return text


def not_a_file(answer, verb, path):
    found = answer["kind"]
    if found == "missing":
        return f"Not {verb}: {path} doesn't exist."
    if found == "directory":
        return f"Not {verb}: {path} is a directory."
    if found == "too_big":
        size = answer["size"] / (1 << 20)
        most = SIZE_CAP >> 20
        return (
            f"Not {verb}: {path} is {size:.0f} MB; the file tools take {most} MB "
            "at most."
        )
    return f"Not {verb}: {path} isn't a regular file."


def lines_of(text):
    """A text's lines without their endings, as `read` numbers them."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def note(text, extra):
    return f"{text}\n\n{extra}" if extra else text


# How much of a write's content or an edit's strings an ask and the judge see
SHOWN = 2000


def clipped(text):
    if len(text) <= SHOWN:
        return text
    return f"{text[:SHOWN]}… [{len(text) - SHOWN:,} more characters]"


def read_summary(args):
    start = args.get("offset") or 1
    if args.get("limit"):
        return f"read {args['path']} lines {start}–{start + args['limit'] - 1}"
    return f"read {args['path']}" + (f" from line {start}" if start > 1 else "")


def write_summary(args):
    content = str(args.get("content", ""))
    count = len(lines_of(content))
    return f"write {args['path']} ({count} lines):\n{clipped(content)}"


def edit_summary(args):
    every = " (every occurrence)" if args.get("replace_all") is True else ""
    return (
        f"edit {args['path']}{every}: replace\n{clipped(str(args['old_string']))}"
        f"\nwith\n{clipped(str(args['new_string']))}"
    )


def read_describe(limits):
    chars = limits.get("read_chars")
    most = f"{chars:,} characters" if chars else "the session's limit of characters"
    return (
        "Read a text file. Each line comes as its number, an arrow and the "
        f"line (12→text); the number and arrow aren't part of the file. At "
        f"most {LINE_CAP} lines and {most} a call; `offset` (the first line, "
        "from 1) and `limit` (how many lines) pick a range."
    )


@tool(
    read_describe,
    optional=("offset", "limit"),
    summary=read_summary,
    # It changes nothing on the machine
    needs_permission=lambda args: False,
    path=("string", "The file: relative to the workspace, or absolute"),
    offset=("integer", "The first line to read, from 1"),
    limit=("integer", f"How many lines to read, {LINE_CAP} at most"),
)
def read(path, offset=None, limit=None, sandbox=None, limits=None, notes=None):
    """`sandbox`, `limits` and `notes` come from the session, never the model:
    its sandbox, its limits (assembly.LIMITS) and the call's notes, where the
    content's hash goes for the stale check."""
    box = sandbox
    for name, value in (("offset", offset), ("limit", limit)):
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 1
        ):
            return f"Not read: {name} is a whole number from 1."
    answer = run_helper(box, {"op": "load", "path": path, "cap": SIZE_CAP})
    if "error" in answer:
        return failed(box, answer, "read", path)
    shown = answer["path"]
    if answer["kind"] != "file":
        return not_a_file(answer, "read", shown)
    data = base64.b64decode(answer["data"])
    if b"\0" in data[:BINARY_PROBE]:
        return f"Not read: {shown} is binary ({len(data):,} bytes)."
    if notes is not None:
        notes["file"] = {
            "path": shown, "real": real_path(box, shown), "seen": answer["sha"],
        }
    lines = lines_of(data.decode("utf-8", errors="replace"))
    if not lines:
        return f"{shown} is empty."
    start = offset or 1
    if start > len(lines):
        return (
            f"Not read: {shown} has {len(lines)} lines; offset {start} is past "
            "its end."
        )
    count = min(limit or LINE_CAP, LINE_CAP)
    budget = limits["read_chars"]
    out = []
    for number in range(start, min(start + count, len(lines) + 1)):
        line = lines[number - 1]
        if len(line) > LONG_LINE:
            line = f"{line[:LONG_LINE]}… [line cut: {len(line):,} characters]"
        text = f"{number}→{line}"
        if out and budget - len(text) - 1 < 0:
            break
        budget -= len(text) + 1
        out.append(text)
    last = start + len(out) - 1
    body = "\n".join(out)
    if last < len(lines):
        total = len(lines)
        body += f"\n\n[Lines {start}–{last} of {total}; offset {last + 1} goes on.]"
    return body


def stale(memory, answer, shown):
    """Why an existing file must not change, or None: it changed since this
    session's tools last saw it."""
    if memory is None:
        return None
    seen = memory.seen(shown)
    if seen is not None and seen != answer.get("sha"):
        return f"Not changed: {shown} changed since this session last read or wrote it."
    return None


def store(box, shown, before, data, notes):
    """Write `data` over `before` (the bytes loaded, or None for a new
    file) and note the change, as hashes. Returns an error text, or None."""
    expect = None if before is None else filehelper.sha(before)
    answer = run_helper(box, {
        "op": "store", "path": shown, "expect": expect,
        "data": base64.b64encode(data).decode(),
    })
    if "error" in answer:
        return failed(box, answer, "changed", shown)
    if answer.get("changed"):
        return f"Not changed: {shown} changed while it was being written."
    if answer["kind"] != "file":
        return not_a_file(answer, "changed", shown)
    real = real_path(box, answer["path"])
    change = {
        "path": shown, "real": real, "before": expect, "after": answer["sha"],
    }
    if notes is not None:
        notes["file"] = change
    return None


LEAVE = (
    "Run this one call outside the sandbox, as the user. It is asked about "
    "first"
)
WHY = "With outside_sandbox: why the task needs it"


def written(args):
    """The file a write or an edit writes: its path, as the call names it."""
    path = args.get("path")
    return [path] if isinstance(path, str) and path else []


# "Don't ask again" for a write or an edit grants its path
PATHS = Grant(
    "paths",
    lambda args: [str(args["path"])] if args.get("path") else None,
    lambda grant: f"{grant['tool']} to " + ", ".join(grant["paths"]),
)


@tool(
    "Write a file whole: created, with any missing folders, or replaced. An "
    "existing file is replaced only when this session has read it and it hasn't "
    "changed since.",
    optional=("outside_sandbox", "reason"),
    summary=write_summary,
    crosses=leaves,
    grant=PATHS,
    writes=written,
    path=("string", "The file: relative to the workspace, or absolute"),
    content=("string", "The file's whole new content"),
    outside_sandbox=("boolean", LEAVE),
    reason=("string", WHY),
)
def write(path, content, outside_sandbox=False, reason="", sandbox=None,
          outside=None, notes=None, files=None):
    """`files`: the session's FileMemory (seen.py), for the stale
    check; None for none (tests calling the tool directly)."""
    box, extra = pick_box(sandbox, outside, outside_sandbox)
    if not isinstance(content, str):
        return "Not changed: content is a string."
    answer = run_helper(box, {"op": "load", "path": path, "cap": SIZE_CAP})
    if "error" in answer:
        return note(failed(box, answer, "changed", path), extra)
    shown = answer["path"]
    before = None
    if answer["kind"] == "file":
        if files is not None and files.seen(shown) is None:
            return note(
                f"Not changed: {shown} exists, and this session hasn't read it.",
                extra,
            )
        problem = stale(files, answer, shown)
        if problem:
            return note(problem, extra)
        before = base64.b64decode(answer["data"])
    elif answer["kind"] != "missing":
        return note(not_a_file(answer, "changed", shown), extra)
    data = content.encode("utf-8")
    problem = store(box, shown, before, data, notes)
    if problem:
        return note(problem, extra)
    count = len(lines_of(content))
    if before is None:
        done = f"Created {shown} ({count} lines)."
    else:
        had = len(lines_of(before.decode("utf-8", errors="replace")))
        done = f"Replaced {shown} ({count} lines; it had {had})."
    return note(done, extra)


@tool(
    "Replace old_string with new_string in a file. It applies when old_string "
    "occurs exactly once, or, with replace_all, at every occurrence. A file "
    "with CRLF line endings is matched with \\n and keeps its endings. A file "
    "that changed since this session last read or wrote it isn't changed.",
    optional=("replace_all", "outside_sandbox", "reason"),
    summary=edit_summary,
    crosses=leaves,
    grant=PATHS,
    writes=written,
    path=("string", "The file: relative to the workspace, or absolute"),
    old_string=("string", "The exact text to replace"),
    new_string=("string", "The text to put in its place"),
    replace_all=("boolean", "Replace every occurrence"),
    outside_sandbox=("boolean", LEAVE),
    reason=("string", WHY),
)
def edit(path, old_string, new_string, replace_all=False, outside_sandbox=False,
         reason="", sandbox=None, outside=None, notes=None, files=None):
    box, extra = pick_box(sandbox, outside, outside_sandbox)
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return "Not changed: old_string and new_string are strings."
    if old_string == "":
        return "Not changed: old_string is empty; write makes or replaces a whole file."
    if old_string == new_string:
        return "Not changed: old_string and new_string are the same."
    answer = run_helper(box, {"op": "load", "path": path, "cap": SIZE_CAP})
    if "error" in answer:
        return note(failed(box, answer, "changed", path), extra)
    shown = answer["path"]
    if answer["kind"] != "file":
        return note(not_a_file(answer, "changed", shown), extra)
    problem = stale(files, answer, shown)
    if problem:
        return note(problem, extra)
    before = base64.b64decode(answer["data"])
    try:
        text = before.decode("utf-8")
    except UnicodeDecodeError:
        return note(f"Not changed: {shown} isn't UTF-8 text.", extra)
    result = replace(text, old_string, new_string, replace_all is True)
    if isinstance(result, str):
        return note(result.format(path=shown), extra)
    new_text, count, first, last = result
    problem = store(box, shown, before, new_text.encode("utf-8"), notes)
    if problem:
        return note(problem, extra)
    lines = f"line {first}" if first == last else f"lines {first}–{last}"
    plural = "" if count == 1 else "s"
    return note(f"Edited {shown}: {lines} ({count} replacement{plural}).", extra)


def replace(text, old, new, every):
    """(new text, replacements, first line, last line of the first one), or
    the reason as text with {path} in it. A file whose every line ends in
    CRLF is matched on its \\n view and keeps CRLF, unless the strings carry
    a \\r of their own (OpenCode, Pi)."""
    crlf = (
        "\r\n" in text and text.count("\n") == text.count("\r\n")
        and "\r" not in old + new
    )
    view = text.replace("\r\n", "\n") if crlf else text
    count = view.count(old)
    if count == 0:
        return "Not changed: old_string isn't in {path}."
    if count > 1 and not every:
        return (
            f"Not changed: old_string occurs {count} times in {{path}}; "
            "replace_all replaces every one."
        )
    at = view.index(old)
    first = view.count("\n", 0, at) + 1
    last = first + new.count("\n")
    view = view.replace(old, new) if every else view.replace(old, new, 1)
    done = view.replace("\n", "\r\n") if crlf else view
    return done, count if every else 1, first, last
