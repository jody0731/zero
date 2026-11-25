"""The file tools: read, write, edit, inspect_file, read_file_part, present_file.

Text disk work runs in sandboxed_io.py through the session's own sandbox so the
walls are the same ones as bash's. Binary and rich file inspection/rendering
runs in reader_worker.py inside a single-layer outer networkless bwrap that
preserves the session Sandbox's hidden covers and reads workspace files and
session attachments in place (zero copying into the attachment store).
"""

import base64
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import paths as home_paths
from core.asking import PARK, TO_APPROVER
from core.tools import Result, tool
from components.attachments import metadata as attachment_metadata
from components.attachments.entries import make_attachment_entry
from components.attachments.store import (
    AttachmentError,
    LimitExceeded,
    short_id_of,
    soft_cap_bytes,
)
from components.files import SETTINGS, reader_worker, sandboxed_io
from components.sandbox.sandbox import (
    NOT_OUTSIDE,
    OUTSIDE_ARGUMENT,
    SandboxError,
    WALL,
    outside_crossing,
)

# The biggest file the text file tools take where no session's limits say.
DEFAULT_MEGABYTES = {s.name: s.default for s in SETTINGS}["max_file_megabytes"]
# A NUL byte in this much of a file's start makes it binary.
BINARY_PROBE_BYTES = 8192
# How long a read or write in the sandbox may run.
SANDBOXED_IO_SECONDS = 30
SANDBOXED_IO_SOURCE = Path(sandboxed_io.__file__).read_text()
# System interpreter for stdlib-only sandboxed_io.
PYTHON = os.path.realpath(sys.executable)
WALL_ERRORS = (errno.EROFS, errno.EACCES, errno.EPERM)
READABLE_IMAGE_TYPES = frozenset({
    "image/png", "image/jpeg", "image/webp", "image/gif",
})
OVER_LIMIT_FACT = (
    "It snapshots a file larger than the soft attachment limit into the "
    "session attachment store."
)


class SandboxedIOError(Exception):
    """sandboxed_io couldn't run, or said nothing readable."""


def run_sandboxed_io(box, request):
    """One request to sandboxed_io, through `box` (a Sandbox, Unsandboxed or
    Outside). The sandbox raises rather than hand back a bare command."""
    # A start the sandbox spoiled ran nothing, so it may go again.
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
    """Where a path sandboxed_io saw is on the machine, or None for scratch."""
    inside = Path(path)
    if not box.sandboxed or inside.is_relative_to(box.workspace):
        return path
    if box.tmp is not None and inside.is_relative_to(box.tmp):
        return None
    return path


def failure_text(box, answer, verb, path):
    """sandboxed_io's OSError, as the model reads it."""
    text = f"Not {verb}: {path}: {answer['error']}."
    if box.sandboxed and answer.get("errno") in WALL_ERRORS:
        text += "\n\n" + WALL.format(walls=box.walls())
    return text


def most_megabytes(limits):
    """The biggest file the text file tools take, in MB."""
    return (limits or {}).get("max_file_megabytes", DEFAULT_MEGABYTES)


def not_a_file_text(answer, verb, path, megabytes=None):
    """Why `path` isn't a file the tools take."""
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
# reads them whole.
SHOWN_CHARS = 2000


def clipped(text, whole=False):
    if whole or len(text) <= SHOWN_CHARS:
        return text
    return f"{text[:SHOWN_CHARS]}… [{len(text) - SHOWN_CHARS:,} more characters]"


def _validate_source_args(args):
    has_path = isinstance(args.get("path"), str) and bool(args["path"].strip())
    has_att = (
        isinstance(args.get("attachment_id"), str)
        and bool(args["attachment_id"].strip())
    )
    if has_path == has_att:
        return "provide exactly one of `path` or `attachment_id`"
    return None


def _resolve_session_attachment(
    attachment_id, session_attachments=None, attachments_dir=None,
):
    """Resolve an authorized session attachment into metadata + readable path."""
    if session_attachments is not None:
        meta = session_attachments.resolve(attachment_id)
        if meta is not None:
            return meta
    if attachments_dir is not None:
        folder = Path(attachments_dir)
        if folder.is_dir():
            prefix = f"{short_id_of(attachment_id)}-"
            matches = sorted(folder.glob(f"{prefix}*"))
            if matches and matches[0].is_file():
                target = matches[0]
                raw = target.read_bytes()
                media = attachment_metadata.media_type(target.name, raw[:4096])
                probed = attachment_metadata.probe(target, media)
                return {
                    "attachment_id": attachment_id,
                    "name": target.name.removeprefix(prefix) or target.name,
                    "media_type": media,
                    "size_bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "path": str(target),
                    "metadata": probed,
                }
    return None


def _active_model_supports_vision(files_model):
    """True only when the active session model explicitly supports image input."""
    if files_model is None:
        return None
    model = files_model() if callable(files_model) else files_model
    if model is None:
        return None
    cap_fn = getattr(model, "capabilities", None)
    caps = cap_fn() if callable(cap_fn) else cap_fn
    if caps is None:
        return None
    return caps.input.get("image")


def _vision_fact_note(files_model):
    """Factual note when visual details are omitted for non-vision models."""
    vision = _active_model_supports_vision(files_model)
    if vision is True:
        return ""
    if vision is False:
        return (
            "The active model does not support image input, so visual details "
            "(layout, colors, charts, typography) in the image are omitted "
            "from what the model sees."
        )
    return (
        "Image input support is unknown for the active model, so visual "
        "details in the image are withheld from model input."
    )


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_WORKER_COPY_CHUNK = 65536
_WORKER_RENDER_MAX_BYTES = 64 * 1024 * 1024


def _copy_worker_output(
    work_dir,
    reported_path,
    *,
    expected_magic=None,
    max_bytes=None,
    copy_dir=None,
):
    """Safely open one worker output relative to `<work_dir>/out` without TOCTOU.

    Rejects paths outside `<work_dir>/out`, traversal, symlinks (`O_NOFOLLOW`),
    and non-regular files (`stat.S_ISREG`), then validates and copies bytes from
    the same opened file descriptor into a fresh host-owned file.
    """
    if not isinstance(reported_path, str) or not reported_path:
        raise reader_worker.ReaderWorkerError(
            "worker reported an empty output path"
        )
    out_root = (Path(work_dir) / "out").resolve()
    raw_path = Path(reported_path)
    if raw_path.is_absolute():
        try:
            rel = raw_path.relative_to(out_root)
        except ValueError:
            raise reader_worker.ReaderWorkerError(
                "worker reported a path outside its private output directory"
            ) from None
    else:
        rel = raw_path
    if (
        len(rel.parts) != 1
        or rel.parts[0] in ("", ".", "..")
        or "/" in rel.parts[0]
        or "\\" in rel.parts[0]
    ):
        raise reader_worker.ReaderWorkerError(
            "worker reported an invalid output filename"
        )
    filename = rel.parts[0]
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
        os, "O_NOFOLLOW", 0,
    )
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        dir_fd = os.open(out_root, dir_flags)
    except OSError as exc:
        raise reader_worker.ReaderWorkerError(
            f"worker output directory unavailable: {exc}"
        ) from None
    try:
        try:
            src_fd = os.open(filename, file_flags, dir_fd=dir_fd)
        except OSError as exc:
            raise reader_worker.ReaderWorkerError(
                f"rejected worker output {filename!r}: {exc}"
            ) from None
    finally:
        os.close(dir_fd)

    host_fd, host_tmp = tempfile.mkstemp(
        prefix="zero-worker-out-", dir=copy_dir,
    )
    host_path = Path(host_tmp)
    try:
        with os.fdopen(src_fd, "rb") as src, os.fdopen(host_fd, "wb") as dst:
            st = os.fstat(src.fileno())
            if not stat.S_ISREG(st.st_mode):
                raise reader_worker.ReaderWorkerError(
                    f"worker output {filename!r} is not a regular file"
                )
            total = 0
            head = bytearray()
            while chunk := src.read(_WORKER_COPY_CHUNK):
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise reader_worker.ReaderWorkerError(
                        f"worker output {filename!r} exceeded {max_bytes:,} bytes"
                    )
                if len(head) < 4096:
                    head.extend(chunk[: 4096 - len(head)])
                dst.write(chunk)
        head_bytes = bytes(head)
        if expected_magic is not None and not head_bytes.startswith(expected_magic):
            raise reader_worker.ReaderWorkerError(
                f"worker output {filename!r} failed magic-header validation"
            )
        return host_path, total, head_bytes
    except BaseException:
        host_path.unlink(missing_ok=True)
        raise


def _persist_rendered_png(
    png_path,
    *,
    name,
    session_attachments=None,
    attachments_dir=None,
    box=None,
    limits=None,
    allow_over_limit=False,
):
    """Validate and persist a rendered PNG into session storage as an Entry."""
    src = Path(png_path)
    probed = attachment_metadata.probe(src, "image/png")
    if session_attachments is not None:
        return session_attachments.add_file(
            src,
            name=name,
            media_hint="image/png",
            delivery="inline",
            allow_over_limit=bool(allow_over_limit),
            limits=limits,
        )
    # Standalone/test fallback without server store: enforce soft cap too.
    raw = src.read_bytes()
    max_file, _ = soft_cap_bytes(limits)
    if not allow_over_limit and max_file is not None and len(raw) > max_file:
        limit_mb = max_file / (1024 * 1024)
        raise LimitExceeded(
            f"attachment size ({len(raw)} bytes) exceeds soft limit "
            f"({limit_mb:g} MiB); set allow_over_limit to override"
        )
    dest_dir = (
        Path(attachments_dir) if attachments_dir is not None
        else (Path(box.private_tmp()) if box is not None and hasattr(box, "private_tmp")
              else src.parent)
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(raw).hexdigest()
    aid = f"att_{digest[:16]}"
    dest = dest_dir / f"{digest[:8]}-{name}"
    if not dest.exists():
        home_paths.write_whole(dest, raw)
    meta = {
        "attachment_id": aid,
        "name": name,
        "media_type": "image/png",
        "size_bytes": len(raw),
        "sha256": digest,
        "path": str(dest),
        "metadata": probed,
    }
    entry = make_attachment_entry(
        aid,
        name=name,
        media_type="image/png",
        size_bytes=len(raw),
        sha256=digest,
        path=str(dest),
        delivery="inline",
        metadata=probed,
    )
    return meta, entry


def read_summary(args):
    target = (
        f"attachment {args['attachment_id']}"
        if args.get("attachment_id")
        else args.get("path", "")
    )
    start = args.get("offset") or 1
    if args.get("limit"):
        return f"read {target} lines {start}–{start + args['limit'] - 1}"
    return f"read {target}" + (f" from line {start}" if start > 1 else "")


def write_summary(args, whole=False):
    content = str(args.get("content", ""))
    count = len(lines_of(content))
    return f"write {args['path']} ({count} lines):\n{clipped(content, whole)}"


def edit_summary(args, whole=False):
    every = " (every occurrence)" if args.get("replace_all") is True else ""
    old = clipped(str(args["old_string"]), whole)
    new = clipped(str(args["new_string"]), whole)
    return f"edit {args['path']}{every}: replace\n{old}\nwith\n{new}"


def inspect_summary(args):
    if args.get("attachment_id"):
        return f"inspect_file attachment {args['attachment_id']}"
    return f"inspect_file {args.get('path', '')}"


def read_part_summary(args):
    target = (
        f"attachment {args['attachment_id']}"
        if args.get("attachment_id")
        else args.get("path", "")
    )
    mode = args.get("mode") or "text"
    bits = [f"mode={mode}"]
    for key in ("pages", "sheet", "range", "member"):
        if args.get(key):
            bits.append(f"{key}={args[key]}")
    return f"read_file_part {target} ({', '.join(bits)})"


def present_summary(args):
    if args.get("attachment_id"):
        return f"present_file attachment {args['attachment_id']}"
    return f"present_file {args.get('path', '')}"


def _over_limit_crossing(args):
    if args.get("allow_over_limit") is not True:
        return None
    return {
        "rule": "attachment_limit",
        "fact": OVER_LIMIT_FACT,
        "why": args.get("reason"),
    }


READ_DESCRIPTION = (
    "Read a text file or session attachment (`attachment_id`). Lines come "
    "numbered (12→text). `offset` (from 1) and `limit` pick a range; with a "
    "limit they come whole, without one within its share of the context's "
    "room. Readable images attach for vision models."
)


@tool(
    READ_DESCRIPTION,
    optional=("path", "offset", "limit", "attachment_id"),
    validate=_validate_source_args,
    summary=read_summary,
    traits=("side_by_side",),
    metadata={
        # Where a part starts and how many lines: texts naming a saved file say
        # how to read it in parts by these.
        "context": {"reads_in_parts": ("offset", "limit")},
        # It changes nothing on the machine.
        "permissions": {"needs_permission": lambda args: False},
    },
    path=("string", "The file: relative to the workspace, or absolute"),
    offset=("integer", "The first line to read, from 1"),
    limit=("integer", "How many lines to read"),
    attachment_id=("string", "A session attachment ID to read"),
)
def read(
    path=None,
    offset=None,
    limit=None,
    attachment_id=None,
    sandbox=None,
    notes=None,
    limits=None,
    session_attachments=None,
    attachments_dir=None,
    files_model=None,
):
    """Read a text file or attachment, or attach a readable image for vision."""
    box = sandbox
    for name, value in (("offset", offset), ("limit", limit)):
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 1
        ):
            return f"Not read: {name} is a whole number from 1."

    att_meta = None
    target_path = path
    if attachment_id:
        att_meta = _resolve_session_attachment(
            attachment_id, session_attachments, attachments_dir,
        )
        if att_meta is None:
            return f"Not read: unknown attachment {attachment_id!r} in this session."
        media = att_meta.get("media_type") or ""
        if media in READABLE_IMAGE_TYPES:
            entry = make_attachment_entry(
                att_meta["attachment_id"],
                name=att_meta["name"],
                media_type=media,
                size_bytes=att_meta["size_bytes"],
                sha256=att_meta["sha256"],
                path=att_meta.get("path"),
                delivery="inline",
                metadata=att_meta.get("metadata"),
            )
            meta_dims = att_meta.get("metadata") or {}
            dims = (
                f" ({meta_dims['width']}x{meta_dims['height']})"
                if meta_dims.get("width") and meta_dims.get("height")
                else ""
            )
            summary = (
                f"Image attachment {att_meta['name']}{dims} "
                f"({media}, {att_meta['size_bytes']:,} bytes, "
                f"id {att_meta['attachment_id']})."
            )
            extra = _vision_fact_note(files_model)
            return Result(note(summary, extra), entries=(entry,))
        target_path = att_meta.get("path")
        if not target_path:
            return f"Not read: attachment {attachment_id!r} has no readable file."

    megabytes = most_megabytes(limits)
    answer = run_sandboxed_io(box, {"op": "load", "path": target_path,
                                    "cap": megabytes << 20})
    if "error" in answer:
        return failure_text(box, answer, "read", target_path)
    shown = answer["path"]
    if answer["kind"] != "file":
        return not_a_file_text(answer, "read", shown, megabytes)
    data = base64.b64decode(answer["data"])
    sniffed = attachment_metadata.media_type(shown, data[:4096])
    if sniffed in READABLE_IMAGE_TYPES and att_meta is None:
        probed = attachment_metadata.probe_bytes(data, sniffed)
        dims = (
            f" ({probed['width']}x{probed['height']})"
            if probed.get("width") and probed.get("height")
            else ""
        )
        entry = make_attachment_entry(
            "",
            name=Path(shown).name or "image",
            media_type=sniffed,
            size_bytes=len(data),
            sha256=answer["sha"],
            path=shown,
            delivery="inline",
            metadata=probed,
        )
        extra = _vision_fact_note(files_model)
        summary = f"Image {shown}{dims} ({sniffed}, {len(data):,} bytes)."
        return Result(note(summary, extra), entries=(entry,))
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
    """Why an existing file must not change, or None."""
    if memory is None:
        return None
    seen = memory.seen(shown)
    if seen is not None and seen != answer.get("sha"):
        return f"Not changed: {shown} changed since this session last read or wrote it."
    return None


def store(box, shown, before, data, notes):
    """Write `data` over `before` and note the change, as hashes."""
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


# "Don't ask again" for a write or an edit grants its path.
PATHS = {
    "field": "paths",
    "of": lambda args: [str(args["path"])] if args.get("path") else None,
    "words": lambda grant: f"{grant['tool']} to " + ", ".join(grant["paths"]),
}


@tool(
    "Write a file whole: created, with any missing folders, or replaced. An "
    "existing file is replaced only when this session has read it and it hasn't "
    "changed since.",
    optional=(OUTSIDE_ARGUMENT, "reason"),
    summary=write_summary,
    metadata={
        "permissions": {"crossing": outside_crossing, "grant": PATHS},
        "files": {"written_files": written_path},
    },
    path=("string", "The file: relative to the workspace, or absolute"),
    content=("string", "The file's whole new content"),
    **{OUTSIDE_ARGUMENT: ("boolean", OUTSIDE_SANDBOX_TEXT)},
    reason=("string", REASON_TEXT),
)
def write(path, content, outside_sandbox=False, reason="", sandbox=None,
          outside=None, notes=None, files=None, limits=None):
    """Write a file whole through the session's sandbox."""
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
    metadata={
        "permissions": {"crossing": outside_crossing, "grant": PATHS},
        "files": {"written_files": written_path},
    },
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
    """Exact-string edit of a UTF-8 file through the session's sandbox."""
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
    the reason as text with {path} in it."""
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


# ------------------------------------------------------------ rich readers


def _resolve_target_for_worker(
    path, attachment_id, session_attachments=None, attachments_dir=None,
):
    """Return `(target_path, display_label, authorized_input, meta, error)`."""
    if attachment_id:
        meta = _resolve_session_attachment(
            attachment_id, session_attachments, attachments_dir,
        )
        if meta is None:
            return (
                None,
                attachment_id,
                False,
                None,
                f"Unknown attachment {attachment_id!r} in this session.",
            )
        target = meta.get("path")
        if not target:
            return (
                None,
                attachment_id,
                False,
                meta,
                f"Attachment {attachment_id!r} has no readable file on disk.",
            )
        return target, meta["name"], True, meta, None
    return path, path, False, None, None


INSPECT_FILE_DESCRIPTION = (
    "Inspect a file's structure and metadata in place (`path` or "
    "`attachment_id`, no copy): PDF, Excel, Word, PowerPoint, images, "
    "audio/video, archives, and text."
)


@tool(
    INSPECT_FILE_DESCRIPTION,
    optional=("path", "attachment_id"),
    validate=_validate_source_args,
    summary=inspect_summary,
    traits=("side_by_side",),
    metadata={"permissions": {"needs_permission": lambda args: False}},
    path=("string", "File path (workspace-relative or absolute)"),
    attachment_id=("string", "Session attachment ID"),
    fields={
        "kind": ("string", "Detected file kind"),
        "media_type": ("string", "Detected MIME type"),
        "size_bytes": ("integer", "File size in bytes"),
        "path": ("string", "Inspected path"),
        "attachment_id": ("string", "Inspected attachment ID"),
    },
)
def inspect_file(
    path=None,
    attachment_id=None,
    sandbox=None,
    session_attachments=None,
    attachments_dir=None,
):
    """Inspect structure and metadata of a file or attachment in place."""
    target, label, is_att, _, error = _resolve_target_for_worker(
        path, attachment_id, session_attachments, attachments_dir,
    )
    if error:
        return Result(f"Not inspected: {error}", failed=True)
    try:
        answer = reader_worker.run_worker(
            sandbox,
            {"op": "inspect", "path": target, "display_path": label},
            authorized_input=is_att,
        )
    except (reader_worker.ReaderWorkerError, SandboxError) as exc:
        return Result(f"Not inspected: {label}: {exc}.", failed=True)
    if not answer.get("ok"):
        return Result(
            f"Not inspected: {label}: {answer.get('error', 'unknown error')}.",
            failed=True,
        )
    fields = {
        "kind": str(answer.get("kind") or "binary"),
        "media_type": str(answer.get("media_type") or "application/octet-stream"),
        "size_bytes": int(answer.get("size_bytes") or 0),
    }
    if attachment_id:
        fields["attachment_id"] = attachment_id
    elif path:
        fields["path"] = path
    return Result(str(answer.get("text") or ""), fields=fields)


READ_FILE_PART_DESCRIPTION = (
    "Read a bounded slice of a file in place (`path` or `attachment_id`): "
    "`mode='text'` for PDF/Office/spreadsheet/archive text and tables, "
    "`mode='render'` for PNG pages/slides/images, `mode='ocr'` for local OCR."
)


@tool(
    READ_FILE_PART_DESCRIPTION,
    optional=(
        "path", "attachment_id", "mode", "pages", "sheet", "range", "member",
        "offset", "limit", "dpi", "crop", "allow_over_limit", "reason",
    ),
    validate=_validate_source_args,
    summary=read_part_summary,
    traits=("side_by_side",),
    metadata={
        "permissions": {
            "needs_permission": lambda args: False,
            "crossing": _over_limit_crossing,
        },
    },
    path=("string", "File path (workspace-relative or absolute)"),
    attachment_id=("string", "Session attachment ID"),
    mode=(
        {"type": "string", "enum": ["text", "render", "ocr"]},
        "Mode: 'text' (default), 'render' (PNG), or 'ocr'",
    ),
    pages=("string", "1-based page/slide range (e.g. '1-3')"),
    sheet=("string", "Spreadsheet sheet name"),
    range=("string", "Spreadsheet range (e.g. 'A1:F30')"),
    member=("string", "Archive member path"),
    offset=("integer", "1-based start line/paragraph/row"),
    limit=("integer", "Max lines/paragraphs/rows"),
    dpi=("integer", "Render DPI (72-200, default 150)"),
    crop=("string", "Image crop 'x,y,width,height'"),
    allow_over_limit=(
        "boolean",
        f"Store rendered previews past the soft cap. Goes first to {TO_APPROVER}",
    ),
    reason=("string", "With allow_over_limit: why"),
    fields={
        "kind": ("string", "Detected file kind"),
        "media_type": ("string", "Detected MIME type"),
        "mode": ("string", "Mode used"),
        "rendered_attachments": (
            {"type": "array", "items": {"type": "string"}},
            "Rendered PNG attachment IDs",
        ),
        "path": ("string", "Path read"),
        "attachment_id": ("string", "Attachment ID read"),
    },
)
def read_file_part(
    path=None,
    attachment_id=None,
    mode="text",
    pages=None,
    sheet=None,
    range=None,
    member=None,
    offset=None,
    limit=None,
    dpi=None,
    crop=None,
    allow_over_limit=False,
    reason="",
    sandbox=None,
    notes=None,
    limits=None,
    session_attachments=None,
    attachments_dir=None,
    files_model=None,
):
    """Read bounded text/tables/slices or render visual PNG pages/slides."""
    if mode not in ("text", "render", "ocr"):
        return Result(
            "Not read: mode must be 'text', 'render', or 'ocr'.", failed=True,
        )
    for arg_name, val in (("offset", offset), ("limit", limit), ("dpi", dpi)):
        if val is not None and (
            not isinstance(val, int) or isinstance(val, bool) or val < 1
        ):
            return Result(
                f"Not read: {arg_name} must be a whole number from 1.",
                failed=True,
            )
    target, label, is_att, _, error = _resolve_target_for_worker(
        path, attachment_id, session_attachments, attachments_dir,
    )
    if error:
        return Result(f"Not read: {error}", failed=True)

    rendered_entries = []
    rendered_ids = []
    stem = Path(label).stem or "render"
    soft_declined = bool(notes and notes.get("soft_limit_declined"))
    hook_approved = bool(notes and notes.get("limit") and not soft_declined)
    override = (allow_over_limit is True) or hook_approved

    def persist_outputs(answer, work_dir):
        if not answer.get("ok"):
            return
        for item in answer.get("rendered") or ():
            if not isinstance(item, dict):
                continue
            png_path = item.get("path")
            if not png_path:
                continue
            host_copy, _, _ = _copy_worker_output(
                work_dir,
                png_path,
                expected_magic=_PNG_MAGIC,
                max_bytes=_WORKER_RENDER_MAX_BYTES,
            )
            try:
                page_no = item.get("page", 1)
                out_name = f"{stem}-p{page_no}.png"
                meta, entry = _persist_rendered_png(
                    host_copy,
                    name=out_name,
                    session_attachments=session_attachments,
                    attachments_dir=attachments_dir,
                    box=sandbox,
                    limits=limits,
                    allow_over_limit=override,
                )
            finally:
                host_copy.unlink(missing_ok=True)
            rendered_entries.append(entry)
            rendered_ids.append(meta["attachment_id"])

    req = {
        "op": "read_part",
        "path": target,
        "display_path": label,
        "mode": mode,
        "pages": pages,
        "sheet": sheet,
        "range": range,
        "member": member,
        "offset": offset,
        "limit": limit,
        "dpi": dpi,
        "crop": crop,
    }
    try:
        answer = reader_worker.run_worker(
            sandbox, req, authorized_input=is_att, on_output=persist_outputs,
        )
    except (
        reader_worker.ReaderWorkerError,
        SandboxError,
        LimitExceeded,
        AttachmentError,
    ) as exc:
        return Result(f"Not read: {label}: {exc}.", failed=True)
    if not answer.get("ok"):
        return Result(
            f"Not read: {label}: {answer.get('error', 'unknown error')}.",
            failed=True,
        )

    text = str(answer.get("text") or "")
    if rendered_entries:
        ids_note = ", ".join(rendered_ids)
        text = f"{text}\nRendered attachment(s): {ids_note}."
        vision_note = _vision_fact_note(files_model)
        if vision_note:
            text = note(text, vision_note)

    fields = {
        "kind": str(answer.get("kind") or "binary"),
        "media_type": str(answer.get("media_type") or "application/octet-stream"),
        "mode": mode,
    }
    if rendered_ids:
        fields["rendered_attachments"] = rendered_ids
    if attachment_id:
        fields["attachment_id"] = attachment_id
    elif path:
        fields["path"] = path
    return Result(text, fields=fields, entries=tuple(rendered_entries))


PRESENT_FILE_DESCRIPTION = (
    "Surface a file (`path` or `attachment_id`) as a deliverable card "
    "(`delivery='reference'`, never sent as native model media). Snapshots "
    "workspace files within soft caps; if declined over cap, references the "
    "workspace path in place."
)


@tool(
    PRESENT_FILE_DESCRIPTION,
    optional=("path", "attachment_id", "title", "allow_over_limit", "reason"),
    validate=_validate_source_args,
    summary=present_summary,
    traits=("side_by_side",),
    metadata={
        "permissions": {
            "needs_permission": lambda args: False,
            "crossing": _over_limit_crossing,
        },
        "tool_boundary": {"keep_stand_ins": ("title",)},
    },
    path=("string", "File path to present"),
    attachment_id=("string", "Existing attachment ID to present"),
    title=("string", "Optional label"),
    allow_over_limit=(
        "boolean",
        f"Snapshot past the soft cap. Goes first to {TO_APPROVER}",
    ),
    reason=("string", "With allow_over_limit: why"),
    fields={
        "delivery": ("string", "Always 'reference'"),
        "snapshotted": ("boolean", "Whether snapshotted into store"),
        "attachment_id": ("string", "Attachment ID if snapshotted"),
        "path": ("string", "Presented file path"),
        "name": ("string", "Display filename"),
        "media_type": ("string", "Detected MIME type"),
        "size_bytes": ("integer", "File size in bytes"),
    },
)
def present_file(
    path=None,
    attachment_id=None,
    title=None,
    allow_over_limit=False,
    reason="",
    sandbox=None,
    notes=None,
    limits=None,
    session_attachments=None,
    attachments_dir=None,
    present_file_ask=None,
):
    """Surface a file as a reference-only deliverable card."""
    if attachment_id:
        meta = _resolve_session_attachment(
            attachment_id, session_attachments, attachments_dir,
        )
        if meta is None:
            return Result(
                f"Not presented: unknown attachment {attachment_id!r} in this session.",
                failed=True,
            )
        display_name = (title or "").strip() or meta["name"]
        entry = make_attachment_entry(
            meta["attachment_id"],
            name=display_name,
            media_type=meta["media_type"],
            size_bytes=meta["size_bytes"],
            sha256=meta["sha256"],
            path=meta.get("path"),
            delivery="reference",
            metadata=meta.get("metadata"),
        )
        fields = {
            "delivery": "reference",
            "snapshotted": True,
            "attachment_id": meta["attachment_id"],
            "name": display_name,
            "media_type": meta["media_type"],
            "size_bytes": meta["size_bytes"],
        }
        if meta.get("path"):
            fields["path"] = meta["path"]
        return Result(
            f"Presented {display_name} ({meta['media_type']}, "
            f"{meta['size_bytes']:,} bytes, attachment {meta['attachment_id']}).",
            fields=fields,
            entries=(entry,),
        )

    soft_declined = bool(notes and notes.get("soft_limit_declined"))
    hook_approved = bool(
        notes and notes.get("limit") and not soft_declined
    )
    override = (allow_over_limit is True) or hook_approved

    def _finish_present(shown, staged_path, size_bytes, head):
        display_name = (title or "").strip() or Path(shown).name or "file"
        media = attachment_metadata.media_type(display_name, head)
        probed = (
            attachment_metadata.probe(Path(staged_path), media)
            if staged_path is not None
            else attachment_metadata.probe_bytes(head, media)
        )

        def _ref_result(why_text):
            ref_entry = make_attachment_entry(
                "",
                name=display_name,
                media_type=media,
                size_bytes=size_bytes,
                sha256="",
                path=shown,
                delivery="reference",
                metadata=probed,
            )
            suffix = f" {why_text}" if why_text else ""
            return Result(
                f"Presented {display_name} ({media}, {size_bytes:,} bytes) at "
                f"{shown}.{suffix}",
                fields={
                    "delivery": "reference",
                    "snapshotted": False,
                    "path": shown,
                    "name": display_name,
                    "media_type": media,
                    "size_bytes": size_bytes,
                },
                entries=(ref_entry,),
            )

        if soft_declined:
            return _ref_result(
                "Snapshotting past the soft attachment cap was declined; "
                "referencing the workspace path in place."
            )

        def _ask_direct(cap_message):
            if (
                notes is None
                and callable(present_file_ask)
                and present_file_ask is not PARK
            ):
                return bool(present_file_ask(
                    f"present_file {shown}",
                    f"snapshot {display_name} ({size_bytes:,} bytes): {cap_message}",
                ))
            return None

        if session_attachments is not None and staged_path is not None:
            try:
                meta, entry = session_attachments.add_file(
                    staged_path,
                    name=display_name,
                    media_hint=media,
                    delivery="reference",
                    allow_over_limit=override,
                    limits=limits,
                )
                return Result(
                    f"Presented {display_name} ({meta['media_type']}, "
                    f"{meta['size_bytes']:,} bytes, attachment "
                    f"{meta['attachment_id']}).",
                    fields={
                        "delivery": "reference",
                        "snapshotted": True,
                        "attachment_id": meta["attachment_id"],
                        "path": meta["path"],
                        "name": display_name,
                        "media_type": meta["media_type"],
                        "size_bytes": meta["size_bytes"],
                    },
                    entries=(entry,),
                )
            except LimitExceeded as exc:
                decision = _ask_direct(str(exc))
                if decision is True:
                    meta, entry = session_attachments.add_file(
                        staged_path,
                        name=display_name,
                        media_hint=media,
                        delivery="reference",
                        allow_over_limit=True,
                        limits=limits,
                    )
                    return Result(
                        f"Presented {display_name} ({meta['media_type']}, "
                        f"{meta['size_bytes']:,} bytes, attachment "
                        f"{meta['attachment_id']}; over-limit snapshot approved).",
                        fields={
                            "delivery": "reference",
                            "snapshotted": True,
                            "attachment_id": meta["attachment_id"],
                            "path": meta["path"],
                            "name": display_name,
                            "media_type": meta["media_type"],
                            "size_bytes": meta["size_bytes"],
                        },
                        entries=(entry,),
                    )
                why = (
                    "Snapshotting past the soft attachment cap was declined; "
                    "referencing the workspace path in place."
                    if decision is False
                    else (
                        f"{exc}. Referencing the workspace path in place without "
                        "snapshotting."
                    )
                )
                return _ref_result(why)
            except AttachmentError as exc:
                return Result(f"Not presented: {shown}: {exc}.", failed=True)

        # Standalone session without a server attachment store.
        max_file, _ = soft_cap_bytes(limits)
        why = ""
        if not override and max_file is not None and size_bytes > max_file:
            limit_mb = max_file / (1024 * 1024)
            cap_msg = (
                f"file size ({size_bytes:,} bytes) exceeds soft limit "
                f"({limit_mb:g} MiB)"
            )
            decision = _ask_direct(cap_msg)
            if decision is False:
                why = (
                    "Snapshotting past the soft attachment cap was declined; "
                    "referencing the workspace path in place."
                )
            elif decision is None:
                why = (
                    f"{cap_msg}; referencing the workspace path in place "
                    "without snapshotting."
                )
        return _ref_result(why)

    # When sandboxed and snapshotting, stage from inside single-layer bwrap to
    # eliminate host-side symlink-swap TOCTOU (zero base64 piping).
    if sandbox is not None and getattr(sandbox, "sandboxed", False):
        if soft_declined or session_attachments is None:
            stat_ans = run_sandboxed_io(sandbox, {"op": "stat", "path": path})
            if "error" in stat_ans:
                return Result(
                    failure_text(sandbox, stat_ans, "presented", path),
                    failed=True,
                )
            shown = stat_ans["path"]
            if stat_ans["kind"] != "file":
                return Result(
                    not_a_file_text(stat_ans, "presented", shown), failed=True,
                )
            head = base64.b64decode(stat_ans.get("head") or "")
            return _finish_present(shown, None, int(stat_ans["size"]), head)

        holder = {}

        def on_staged(answer, work_dir):
            if not answer.get("ok"):
                return
            shown = answer["path"]
            host_copy, real_size, real_head = _copy_worker_output(
                work_dir,
                answer.get("staged_path"),
                max_bytes=reader_worker.FSIZE_LIMIT_BYTES,
            )
            try:
                holder["result"] = _finish_present(
                    shown, str(host_copy), real_size, real_head,
                )
            finally:
                host_copy.unlink(missing_ok=True)

        try:
            answer = reader_worker.run_worker(
                sandbox, {"op": "stage", "path": path}, on_output=on_staged,
            )
        except (reader_worker.ReaderWorkerError, SandboxError) as exc:
            return Result(f"Not presented: {path}: {exc}.", failed=True)
        if "result" in holder:
            return holder["result"]
        shown = answer.get("path") or path
        kind = answer.get("kind")
        if kind in ("missing", "directory", "other", "too_big"):
            return Result(not_a_file_text(answer, "presented", shown), failed=True)
        return Result(
            f"Not presented: {shown}: {answer.get('error', 'unknown error')}.",
            failed=True,
        )

    # Unsandboxed / Outside mode: stat and present directly.
    stat_ans = run_sandboxed_io(sandbox, {"op": "stat", "path": path})
    if "error" in stat_ans:
        return Result(failure_text(sandbox, stat_ans, "presented", path), failed=True)
    shown = stat_ans["path"]
    if stat_ans["kind"] != "file":
        return Result(not_a_file_text(stat_ans, "presented", shown), failed=True)
    head = base64.b64decode(stat_ans.get("head") or "")
    return _finish_present(shown, shown, int(stat_ans["size"]), head)


# What this component offers the model, in the order it reads them.
TOOLS = (read, write, edit, inspect_file, read_file_part, present_file)
