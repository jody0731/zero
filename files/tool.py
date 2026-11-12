"""The file tools: read with line ranges, write, exact-string edit.

Their disk work runs in sandboxed_io.py through the session's own sandbox,
as bash's commands do, so the walls are the same ones (Codex runs apply_patch
this way): no second set of path checks. Where the mode has no sandbox, or a
call's ask to leave it was approved, sandboxed_io runs bare, as the user. Each
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

from core.asking import TO_APPROVER
from core.tools import Grant, tool
from files import SETTINGS, sandboxed_io
from sandbox.sandbox import NOT_OUTSIDE, OUTSIDE_ARGUMENT, WALL, outside_crossing

# The biggest file the file tools take where no session's limits say (a call
# made without a session), as the setting declares it
DEFAULT_MEGABYTES = {s.name: s.default for s in SETTINGS}["max_file_megabytes"]
# A NUL byte in this much of a file's start makes it binary
BINARY_PROBE_BYTES = 8192
# How long a read or write in the sandbox may run
SANDBOXED_IO_SECONDS = 30
SANDBOXED_IO_SOURCE = Path(sandboxed_io.__file__).read_text()
# The system's own interpreter, not the venv's: the sandbox may hide the venv
# (the evals hide the whole repository); sandboxed_io needs only the stdlib
PYTHON = os.path.realpath(sys.executable)
WALL_ERRORS = (errno.EROFS, errno.EACCES, errno.EPERM)


class SandboxedIOError(Exception):
    """sandboxed_io couldn't run, or said nothing readable."""


def run_sandboxed_io(box, request):
    """One request to sandboxed_io, through `box` (a Sandbox, Unsandboxed or
    Outside). The sandbox raises rather than hand back a bare command."""
    # A start the sandbox spoiled ran nothing, so it may go again
    tries = 0
    while True:
        tries += 1
        done = subprocess.run(
            box.command([PYTHON, "-I", "-c", SANDBOXED_IO_SOURCE]),
            input=json.dumps(request),
            capture_output=True,
            text=True,
            env=box.env(),
            cwd=box.workspace,
            timeout=SANDBOXED_IO_SECONDS,
        )
        if not box.start_again(tries, done.returncode, done.stderr):
            break
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError:
        tail = done.stderr.strip()[-300:] or f"exit code {done.returncode}"
        raise SandboxedIOError(
            f"the file operation in the sandbox failed: {tail}"
        ) from None


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
    """Where a path sandboxed_io saw is on the machine: the same path, as the
    sandbox binds the workspace and the scratch folder at their own; None
    for one in the scratch folder, kept only until the server restarts, which
    the restart note doesn't look at."""
    inside = Path(path)
    if not box.sandboxed or inside.is_relative_to(box.workspace):
        return path
    if box.tmp is not None and inside.is_relative_to(box.tmp):
        return None
    return path


def failure_text(box, answer, verb, path):
    """sandboxed_io's OSError, as the model reads it; the sandbox's walls named
    when they are what stopped it."""
    text = f"Not {verb}: {path}: {answer['error']}."
    if box.sandboxed and answer.get("errno") in WALL_ERRORS:
        text += "\n\n" + WALL.format(walls=box.walls())
    return text


def most_megabytes(limits):
    """The biggest file the file tools take, in MB, as the session's limits
    stand: bigger is left to commands, since the file tools carry a file
    whole between the box and the server."""
    return (limits or {}).get("max_file_megabytes", DEFAULT_MEGABYTES)


def not_a_file_text(answer, verb, path, megabytes=None):
    """Why `path` isn't a file the tools take; `megabytes`: their size cap,
    for a file past it."""
    found = answer["kind"]
    if found == "missing":
        return f"Not {verb}: {path} doesn't exist."
    if found == "directory":
        return f"Not {verb}: {path} is a directory."
    if found == "too_big":
        size = answer["size"] / (1 << 20)
        return (
            f"Not {verb}: {path} is {size:.0f} MB; the file tools take "
            f"{megabytes} MB at most."
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


# How much of a write's content or an edit's strings an ask shows; the judge
# reads them whole
SHOWN_CHARS = 2000


def clipped(text, whole=False):
    if whole or len(text) <= SHOWN_CHARS:
        return text
    return f"{text[:SHOWN_CHARS]}… [{len(text) - SHOWN_CHARS:,} more characters]"


def read_summary(args):
    start = args.get("offset") or 1
    if args.get("limit"):
        return f"read {args['path']} lines {start}–{start + args['limit'] - 1}"
    return f"read {args['path']}" + (f" from line {start}" if start > 1 else "")


def write_summary(args, whole=False):
    content = str(args.get("content", ""))
    count = len(lines_of(content))
    return f"write {args['path']} ({count} lines):\n{clipped(content, whole)}"


def edit_summary(args, whole=False):
    every = " (every occurrence)" if args.get("replace_all") is True else ""
    old = clipped(str(args["old_string"]), whole)
    new = clipped(str(args["new_string"]), whole)
    return f"edit {args['path']}{every}: replace\n{old}\nwith\n{new}"


READ_DESCRIPTION = (
    "Read a text file. Each line comes as its number, an arrow and the line "
    "(12→text); the number and arrow aren't part of the file. `offset` (the "
    "first line, from 1) and `limit` (how many lines) pick a range. With a "
    "limit, those lines come whole. Without one, the file from offset to its "
    "end comes within its share of the context's room; past it, its start "
    "and end come with its size."
)


@tool(
    READ_DESCRIPTION,
    # Where a part starts and how many lines: texts naming a saved file say
    # how to read it in parts by these
    reads_in_parts=("offset", "limit"),
    optional=("offset", "limit"),
    summary=read_summary,
    # It changes nothing on the machine
    needs_permission=lambda args: False,
    side_by_side=True,
    path=("string", "The file: relative to the workspace, or absolute"),
    offset=("integer", "The first line to read, from 1"),
    limit=("integer", "How many lines to read"),
)
def read(path, offset=None, limit=None, sandbox=None, notes=None, limits=None):
    """`sandbox`, `notes` and `limits` come from the session, never the
    model: its sandbox, the call's notes, where the content's hash goes for
    the stale check, and what the context component reads of the result (a
    read with a limit is sized by ZERO, `sized`; one without names the file
    that holds it whole, `whole_at`, so one past its share isn't copied),
    and the session's limits (the size cap; `limit` is the model's, lines)."""
    box = sandbox
    for name, value in (("offset", offset), ("limit", limit)):
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 1
        ):
            return f"Not read: {name} is a whole number from 1."
    megabytes = most_megabytes(limits)
    answer = run_sandboxed_io(box, {"op": "load", "path": path,
                                    "cap": megabytes << 20})
    if "error" in answer:
        return failure_text(box, answer, "read", path)
    shown = answer["path"]
    if answer["kind"] != "file":
        return not_a_file_text(answer, "read", shown, megabytes)
    data = base64.b64decode(answer["data"])
    if b"\0" in data[:BINARY_PROBE_BYTES]:
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
    if notes is not None:
        if limit is not None:
            notes["sized"] = True
        else:
            notes["whole_at"] = shown
    last = len(lines) if limit is None else min(start + limit - 1, len(lines))
    body = "\n".join(f"{n}→{lines[n - 1]}" for n in range(start, last + 1))
    if last < len(lines):
        total, after = len(lines), last + 1
        body += f"\n\n[Lines {start}–{last} of {total}; offset {after} goes on.]"
    return body


def stale_reason(memory, answer, shown):
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
    expect = None if before is None else sandboxed_io.sha(before)
    answer = run_sandboxed_io(box, {
        "op": "store", "path": shown, "expect": expect,
        "data": base64.b64encode(data).decode(),
    })
    if "error" in answer:
        return failure_text(box, answer, "changed", shown)
    if answer.get("changed"):
        return f"Not changed: {shown} changed while it was being written."
    if answer["kind"] != "file":
        return not_a_file_text(answer, "changed", shown)
    real = real_path(box, answer["path"])
    change = {
        "path": shown, "real": real, "before": expect, "after": answer["sha"],
    }
    if notes is not None:
        notes["file"] = change
    return None


OUTSIDE_SANDBOX_TEXT = (
    f"Run this one call outside the sandbox, as the user. It goes first to "
    f"{TO_APPROVER}"
)
REASON_TEXT = f"With {OUTSIDE_ARGUMENT}: why the task needs it"


def written_path(args):
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
    optional=(OUTSIDE_ARGUMENT, "reason"),
    summary=write_summary,
    crossing=outside_crossing,
    grant=PATHS,
    written_files=written_path,
    path=("string", "The file: relative to the workspace, or absolute"),
    content=("string", "The file's whole new content"),
    **{OUTSIDE_ARGUMENT: ("boolean", OUTSIDE_SANDBOX_TEXT)},
    reason=("string", REASON_TEXT),
)
def write(path, content, outside_sandbox=False, reason="", sandbox=None,
          outside=None, notes=None, files=None, limits=None):
    """`files`: the session's SeenFiles (seen.py), for the stale
    check; None for none (tests calling the tool directly). `limits`: the
    session's (the size cap); None for the setting's default."""
    box, extra = pick_box(sandbox, outside, outside_sandbox)
    if not isinstance(content, str):
        return "Not changed: content is a string."
    megabytes = most_megabytes(limits)
    answer = run_sandboxed_io(box, {"op": "load", "path": path,
                                    "cap": megabytes << 20})
    if "error" in answer:
        return note(failure_text(box, answer, "changed", path), extra)
    shown = answer["path"]
    before = None
    if answer["kind"] == "file":
        if files is not None and files.seen(shown) is None:
            return note(
                f"Not changed: {shown} exists, and this session hasn't read it.",
                extra,
            )
        problem = stale_reason(files, answer, shown)
        if problem:
            return note(problem, extra)
        before = base64.b64decode(answer["data"])
    elif answer["kind"] != "missing":
        return note(not_a_file_text(answer, "changed", shown, megabytes), extra)
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
    optional=("replace_all", OUTSIDE_ARGUMENT, "reason"),
    summary=edit_summary,
    crossing=outside_crossing,
    grant=PATHS,
    written_files=written_path,
    path=("string", "The file: relative to the workspace, or absolute"),
    old_string=("string", "The exact text to replace"),
    new_string=("string", "The text to put in its place"),
    replace_all=("boolean", "Replace every occurrence"),
    **{OUTSIDE_ARGUMENT: ("boolean", OUTSIDE_SANDBOX_TEXT)},
    reason=("string", REASON_TEXT),
)
def edit(path, old_string, new_string, replace_all=False, outside_sandbox=False,
         reason="", sandbox=None, outside=None, notes=None, files=None,
         limits=None):
    """`limits`: the session's (the size cap); None for the setting's
    default."""
    box, extra = pick_box(sandbox, outside, outside_sandbox)
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return "Not changed: old_string and new_string are strings."
    if old_string == "":
        return "Not changed: old_string is empty; write makes or replaces a whole file."
    if old_string == new_string:
        return "Not changed: old_string and new_string are the same."
    megabytes = most_megabytes(limits)
    answer = run_sandboxed_io(box, {"op": "load", "path": path,
                                    "cap": megabytes << 20})
    if "error" in answer:
        return note(failure_text(box, answer, "changed", path), extra)
    shown = answer["path"]
    if answer["kind"] != "file":
        return note(not_a_file_text(answer, "changed", shown, megabytes), extra)
    problem = stale_reason(files, answer, shown)
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


# What this component offers the model, in the order it reads them; the
# assembly adds them to core's registry (core/tools.py, add)
TOOLS = (read, write, edit)
