"""Single-layer outer networkless bwrap reader worker for binary & rich files.

Runs untrusted file parsing and visual page/slide rendering in a short-lived
subprocess inside ONE outer networkless bwrap (never nested inside Sandbox, and
never piping whole files as base64 over stdin/stdout):
- Preserves the session Sandbox's hidden covers AFTER mounting workspace so
  private files and secrets stay hidden.
- Reads workspace paths and session attachments in place (read-only), with zero
  copying into the attachment store.
- Mounts a private disposable writable temp dir for LibreOffice profiles and
  rendered PNG handoff.
- Enforces independent CPU, memory, file-size, wall-time, output-size, and
  archive-expansion caps independent of user storage caps.
- Imports MIT/permissive reader packages ONLY inside worker functions executed
  in the subprocess, keeping ZERO's main server free-threaded (GIL off).
"""

import base64
import csv
import datetime
import gzip
import hashlib
import io
import json
import os
import posixpath
import re
import resource
import shutil
import site
import stat
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

# Independent worker resource and expansion caps (never tied to storage caps).
WORKER_TIMEOUT_SECONDS = 45
RENDER_TIMEOUT_SECONDS = 90
CPU_LIMIT_SECONDS = 30
RENDER_CPU_LIMIT_SECONDS = 60
MEMORY_LIMIT_BYTES = 4 * 1024 * 1024 * 1024
RENDER_MEMORY_LIMIT_BYTES = 6 * 1024 * 1024 * 1024
FSIZE_LIMIT_BYTES = 128 * 1024 * 1024
MAX_STDOUT_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 60_000
HARD_TEXT_CHARS = 120_000
MAX_RENDER_PAGES = 6
DEFAULT_RENDER_DPI = 150
MIN_RENDER_DPI = 72
MAX_RENDER_DPI = 200
MAX_RENDER_DIM = 2048
MAX_IMAGE_PIXELS = 40_000_000
MAX_ARCHIVE_ENTRIES = 2000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_RATIO = 100.0
MAX_SHEET_ROWS = 200
MAX_SHEET_COLS = 50
MAX_OOXML_XML_BYTES = 16 * 1024 * 1024
MAX_TEXT_INSPECT_BYTES = 4 * 1024 * 1024
BINARY_PROBE_BYTES = 8192

WORKER_SCRIPT = Path(__file__).resolve()

OFFICE_EXTENSIONS = frozenset({".docx", ".pptx", ".xlsx", ".xlsm"})
IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".ico",
})
HEIF_EXTENSIONS = frozenset({".heic", ".heif"})
AUDIO_EXTENSIONS = frozenset({
    ".wav", ".mp3", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac", ".wma",
    ".aiff", ".aif",
})
VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".mpeg", ".mpg", ".wmv",
    ".flv", ".3gp",
})
ARCHIVE_SUFFIXES = (
    ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar", ".zip",
    ".gz",
)

# XML namespaces used by OOXML Word (.docx) and PowerPoint (.pptx) packages.
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DC_NS = "http://purl.org/dc/elements/1.1/"
CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"


class ReaderWorkerError(Exception):
    """The isolated reader worker failed or returned an unreadable reply."""


def _python_ro_binds():
    """Deduplicated read-only host paths needed by the isolated Python worker."""
    import paths as home_paths

    user_home = Path.home().resolve()
    zero_home = home_paths.home().resolve()
    excluded = {
        Path("/"),
        Path("/usr"),
        user_home,
        (user_home / ".local").resolve(),
        zero_home,
    }
    candidates = []
    release = home_paths.release_folder()
    if release is not None:
        candidates.append(release / home_paths.PYTHON_FOLDER)
    for raw in (sys.prefix, sys.base_prefix):
        if raw:
            candidates.append(Path(raw))
    exe = Path(sys.executable)
    candidates.append(exe.parent.parent)
    candidates.append(exe.resolve().parent.parent)
    try:
        for sp in site.getsitepackages():
            candidates.append(Path(sp))
    except Exception:
        pass
    seen = set()
    ordered = []
    for item in candidates:
        try:
            resolved = item.resolve()
        except OSError:
            continue
        if not resolved.exists() or resolved in excluded:
            continue
        if user_home.is_relative_to(resolved) or zero_home.is_relative_to(resolved):
            continue
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            ordered.append(key)
        raw_key = str(item)
        if raw_key != key and item.exists() and raw_key not in seen:
            if item not in excluded:
                seen.add(raw_key)
                ordered.append(raw_key)
    return ordered


def _safe_input_basename(path):
    name = Path(path).name or "input"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).lstrip(".")
    return cleaned[:120] or "input"


def build_worker_command(
    box,
    *,
    target_path,
    work_dir,
    authorized_input=False,
    allow_bare_fallback=False,
):
    """Build ONE single-layer outer networkless bwrap command for the worker.

    Returns `(argv, env, worker_input_path, used_bwrap)`.
    Never nests bwrap inside Sandbox and never weakens sandboxed modes.
    """
    from components.sandbox.sandbox import (
        ISOLATION,
        POINTERS,
        SandboxError,
        harness_free_env,
        tmp_socket_holders,
    )

    work = Path(work_dir)
    for sub in ("out", "home", "tmp", "config", "cache", "input"):
        (work / sub).mkdir(parents=True, exist_ok=True)
    work_script = work / "_worker.py"
    work_script.touch(exist_ok=True)

    env = harness_free_env()
    for name in POINTERS:
        env.pop(name, None)
    env["HOME"] = str(work / "home")
    env["TMPDIR"] = str(work / "tmp")
    env["XDG_CONFIG_HOME"] = str(work / "config")
    env["XDG_CACHE_HOME"] = str(work / "cache")
    env["OPENBLAS_NUM_THREADS"] = "1"
    env["OMP_NUM_THREADS"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    sandboxed = bool(getattr(box, "sandboxed", True))
    bwrap = shutil.which("bwrap")
    if not bwrap:
        if sandboxed and not allow_bare_fallback:
            raise SandboxError("bwrap is not installed, so nothing can run")
        return (
            [sys.executable, "-I", str(WORKER_SCRIPT)],
            env,
            str(target_path),
            False,
        )

    workspace = Path(box.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    ws = str(workspace)
    args = [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]

    # Preserve Sandbox covered_folders before restoring Python & workspace.
    if hasattr(box, "covered_folders"):
        for folder in box.covered_folders():
            if Path(folder).exists():
                args += ["--tmpfs", str(folder)]

    # Expose Python runtime & site-packages BEFORE post-workspace hiding so
    # nothing in _python_ro_binds can uncover secrets or ZERO's home.
    for py_path in _python_ro_binds():
        args += ["--ro-bind-try", py_path, py_path]

    # Cover /tmp socket holders BEFORE workspace & session scratch so a
    # workspace or scratch dir living under /tmp/<holder> is not wiped out.
    for holder in tmp_socket_holders():
        if holder.is_dir():
            args += ["--tmpfs", str(holder)]
        elif os.path.lexists(holder):
            args += ["--ro-bind", "/dev/null", str(holder)]

    # Mount workspace and session scratch read-only (never writable in reader).
    if workspace != Path("/"):
        args += ["--ro-bind", ws, ws]
    if hasattr(box, "private_tmp"):
        session_tmp = box.private_tmp()
        if session_tmp and Path(session_tmp).exists():
            args += ["--ro-bind", str(session_tmp), str(session_tmp)]

    # Apply secret, private, and system-socket covers AFTER workspace so
    # hidden children stay hidden.
    strict_box = box.without_changes() if hasattr(box, "without_changes") else None
    if strict_box is not None and hasattr(strict_box, "hiding"):
        args += strict_box.hiding()
    elif hasattr(box, "hiding"):
        args += box.hiding()

    # Restore explicitly shown read-only folders (attachments, skills, results).
    if hasattr(box, "shown_again"):
        args += box.shown_again()

    # Remount all empty tmpfs covers read-only before mounting private work_dir.
    tmpfs_mounts = [
        args[i + 1] for i, val in enumerate(args) if val == "--tmpfs"
    ]
    for mount in tmpfs_mounts:
        args += ["--remount-ro", mount]

    # Bind only the private disposable work directory writable, plus the
    # read-only worker script inside it.
    args += ["--bind", str(work), str(work)]
    args += ["--ro-bind", str(WORKER_SCRIPT), str(work_script)]

    worker_input_path = str(target_path)
    if authorized_input:
        real_input = Path(target_path).resolve()
        mount_input = work / "input" / _safe_input_basename(target_path)
        mount_input.touch(exist_ok=True)
        args += ["--ro-bind", str(real_input), str(mount_input)]
        worker_input_path = str(mount_input)

    args += [*ISOLATION, "--disable-userns", "--chdir", ws, "--"]
    args += [sys.executable, "-I", str(work_script)]
    return args, env, worker_input_path, True


def run_worker(
    box,
    request,
    *,
    authorized_input=False,
    on_output=None,
    timeout=None,
    allow_bare_fallback=False,
):
    """Run one reader worker request in a disposable outer networkless bwrap.

    `on_output(answer, work_dir)` runs BEFORE the private temp dir is removed
    so callers can validate and persist rendered PNGs into the attachment store.
    """
    is_render = request.get("mode") == "render"
    wall_timeout = (
        timeout if timeout is not None
        else (RENDER_TIMEOUT_SECONDS if is_render else WORKER_TIMEOUT_SECONDS)
    )
    work_dir = tempfile.mkdtemp(prefix="zero-reader-")
    try:
        tries = 0
        while True:
            tries += 1
            cmd, env, worker_path, used_bwrap = build_worker_command(
                box,
                target_path=request["path"],
                work_dir=work_dir,
                authorized_input=authorized_input,
                allow_bare_fallback=allow_bare_fallback,
            )
            payload = {
                **request,
                "path": worker_path,
                "display_path": request.get("display_path") or request["path"],
                "work_dir": work_dir,
                "render_mode": is_render,
            }
            try:
                done = subprocess.run(
                    cmd,
                    input=json.dumps(payload).encode("utf-8"),
                    capture_output=True,
                    env=env,
                    cwd=str(box.workspace),
                    timeout=wall_timeout,
                )
            except subprocess.TimeoutExpired:
                raise ReaderWorkerError(
                    f"file reader did not finish within {wall_timeout}s"
                ) from None
            stderr_text = done.stderr.decode("utf-8", errors="replace")
            if used_bwrap and hasattr(box, "start_again") and box.start_again(
                tries, done.returncode, stderr_text,
            ):
                continue
            break

        if len(done.stdout) > MAX_STDOUT_BYTES:
            raise ReaderWorkerError(
                f"file reader output exceeded {MAX_STDOUT_BYTES:,} byte cap"
            )
        try:
            answer = json.loads(done.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            tail = stderr_text.strip()[-300:] or f"exit code {done.returncode}"
            raise ReaderWorkerError(
                f"the isolated file reader failed: {tail}"
            ) from None
        if not isinstance(answer, dict):
            raise ReaderWorkerError("the isolated file reader returned invalid data")
        if on_output is not None:
            on_output(answer, Path(work_dir))
        return answer
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ------------------------------------------------------------ worker runtime


def _apply_resource_limits(render_mode=False, stage_mode=False):
    cpu = RENDER_CPU_LIMIT_SECONDS if render_mode else CPU_LIMIT_SECONDS
    mem = RENDER_MEMORY_LIMIT_BYTES if render_mode else MEMORY_LIMIT_BYTES
    limits = [
        (resource.RLIMIT_CPU, cpu),
        (resource.RLIMIT_AS, mem),
    ]
    if not stage_mode:
        limits.append((resource.RLIMIT_FSIZE, FSIZE_LIMIT_BYTES))
    for kind, limit in limits:
        try:
            soft, hard = resource.getrlimit(kind)
            if hard != resource.RLIM_INFINITY:
                limit = min(limit, hard)
            if soft == resource.RLIM_INFINITY or soft > limit:
                resource.setrlimit(kind, (limit, hard))
        except (OSError, ValueError):
            pass


def _clamp_text(text, max_chars=MAX_TEXT_CHARS):
    cap = min(max(1000, int(max_chars)), HARD_TEXT_CHARS)
    if len(text) <= cap:
        return text, False
    cut = text[:cap]
    extra = len(text) - cap
    note = f"[Output truncated at {cap:,} characters; {extra:,} more omitted.]"
    return f"{cut}\n\n{note}", True


def _detect_kind(path, head):
    lower = str(path).lower()
    if head.startswith(b"%PDF-"):
        return "pdf", "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image", "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF":
        if head[8:12] == b"WEBP":
            return "image", "image/webp"
        if head[8:12] == b"WAVE":
            return "audio", "audio/wav"
        if head[8:12] == b"AVI ":
            return "video", "video/x-msvideo"
    if head.startswith(b"BM") and lower.endswith(".bmp"):
        return "image", "image/bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image", "image/tiff"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
            return "heif", "image/heic"
        if brand in (b"M4A ", b"M4B "):
            return "audio", "audio/mp4"
        if brand == b"qt  ":
            return "video", "video/quicktime"
        return "video", "video/mp4"
    if head.startswith(b"OggS"):
        return "audio", "audio/ogg"
    if head.startswith(b"fLaC"):
        return "audio", "audio/flac"
    if head.startswith(b"ID3") or (
        len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0
        and lower.endswith(".mp3")
    ):
        return "audio", "audio/mpeg"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        if lower.endswith(".webm"):
            return "video", "video/webm"
        return "video", "video/x-matroska"

    ext = os.path.splitext(lower)[1]
    if ext in HEIF_EXTENSIONS:
        return "heif", "image/heic"
    if ext == ".pdf":
        return "pdf", "application/pdf"
    if ext in (".xlsx", ".xlsm"):
        return "xlsx", (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    if ext == ".docx":
        return "docx", (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        )
    if ext == ".pptx":
        return "pptx", (
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation"
        )
    if ext in IMAGE_EXTENSIONS:
        return "image", f"image/{ext.lstrip('.')}"
    if ext in AUDIO_EXTENSIONS:
        return "audio", f"audio/{ext.lstrip('.')}"
    if ext in VIDEO_EXTENSIONS:
        return "video", f"video/{ext.lstrip('.')}"
    for suffix in ARCHIVE_SUFFIXES:
        if lower.endswith(suffix):
            if suffix == ".zip":
                return "archive", "application/zip"
            if suffix == ".gz":
                return "archive", "application/gzip"
            return "archive", "application/x-tar"
    if head.startswith(b"PK\x03\x04"):
        return "archive", "application/zip"
    if head.startswith(b"\x1f\x8b"):
        return "archive", "application/gzip"
    if b"\0" not in head[:BINARY_PROBE_BYTES]:
        mime = "text/csv" if ext == ".csv" else (
            "application/json" if ext == ".json" else "text/plain"
        )
        return "text", mime
    return "binary", "application/octet-stream"


def _parse_page_spec(spec, total, *, max_count=None):
    """Parse 1-based page/slide specs like '1', '1-3', '2,4-6' into sorted ints."""
    if total <= 0:
        return []
    if spec is None or str(spec).strip() == "":
        end = total if max_count is None else min(total, max_count)
        return list(range(1, end + 1))
    raw = str(spec).strip()
    pages = []
    seen = set()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            left, _, right = token.partition("-")
            if not left.strip().isdigit() or not right.strip().isdigit():
                raise ValueError(f"invalid page range {token!r}")
            a, b = int(left.strip()), int(right.strip())
            if a < 1 or b < a:
                raise ValueError(f"invalid page range {token!r}")
            for p in range(a, min(b, total) + 1):
                if p not in seen:
                    seen.add(p)
                    pages.append(p)
        else:
            if not token.isdigit():
                raise ValueError(f"invalid page number {token!r}")
            p = int(token)
            if p < 1:
                raise ValueError(f"page number must be >= 1, got {p}")
            if p <= total and p not in seen:
                seen.add(p)
                pages.append(p)
    if max_count is not None and len(pages) > max_count:
        pages = pages[:max_count]
    return pages


def _md_escape(value):
    text = "" if value is None else str(value)
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    return text.replace("|", "\\|")


def _render_markdown_table(rows):
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    if width == 0:
        return ""
    norm = [list(r) + [""] * (width - len(r)) for r in rows]
    header = "| " + " | ".join(_md_escape(c) for c in norm[0]) + " |"
    sep = "| " + " | ".join("---" for _ in range(width)) + " |"
    body = [
        "| " + " | ".join(_md_escape(c) for c in row) + " |"
        for row in norm[1:]
    ]
    return "\n".join([header, sep, *body])


def _run_tesseract(png_path):
    binary = shutil.which("tesseract")
    if not binary:
        return None, "tesseract is not installed on this machine"
    try:
        done = subprocess.run(
            [binary, str(png_path), "stdout", "--psm", "6"],
            capture_output=True,
            text=True,
            timeout=25,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"tesseract failed: {exc}"
    if done.returncode != 0:
        err = done.stderr.strip()[-200:] or f"exit code {done.returncode}"
        return None, f"tesseract failed: {err}"
    return done.stdout.strip(), None


def _convert_office_to_pdf(src_path, work_dir):
    soffice = shutil.which("soffice")
    if not soffice:
        return None, (
            "soffice (LibreOffice) is not installed on this machine; "
            "visual Office rendering is unavailable (text/XML inspection works)"
        )
    lo_profile = Path(work_dir) / "lo-profile"
    lo_out = Path(work_dir) / "lo-pdf"
    lo_profile.mkdir(parents=True, exist_ok=True)
    lo_out.mkdir(parents=True, exist_ok=True)
    cmd = [
        soffice,
        "--headless",
        "--nologo",
        "--nodefault",
        "--nolockcheck",
        "--norestore",
        f"-env:UserInstallation=file://{lo_profile}",
        "--convert-to",
        "pdf",
        "--outdir",
        str(lo_out),
        str(src_path),
    ]
    try:
        done = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"soffice conversion failed: {exc}"
    pdfs = sorted(lo_out.glob("*.pdf"))
    if done.returncode != 0 or not pdfs:
        err = done.stderr.strip()[-240:] or f"exit code {done.returncode}"
        return None, f"soffice conversion failed: {err}"
    return pdfs[0], None


def _render_pdf_pages(pdf_path, pages, dpi, out_dir, prefix="page"):
    import pypdfium2 as pdfium
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    dpi = max(MIN_RENDER_DPI, min(int(dpi or DEFAULT_RENDER_DPI), MAX_RENDER_DPI))
    scale = dpi / 72.0
    rendered = []
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        total = len(doc)
        chosen = _parse_page_spec(pages, total, max_count=MAX_RENDER_PAGES)
        for pno in chosen:
            page = doc[pno - 1]
            try:
                bitmap = page.render(scale=scale)
                image = bitmap.to_pil()
                if max(image.size) > MAX_RENDER_DIM:
                    image.thumbnail((MAX_RENDER_DIM, MAX_RENDER_DIM))
                out_file = Path(out_dir) / f"{prefix}-{pno}.png"
                image.save(out_file, format="PNG")
                rendered.append({
                    "page": pno,
                    "path": str(out_file),
                    "name": out_file.name,
                    "width": image.width,
                    "height": image.height,
                })
            finally:
                page.close()
    finally:
        doc.close()
    return rendered, total


# ------------------------------------------------------------ PDF reader


def _inspect_pdf(path):
    import pdfplumber
    import pypdfium2 as pdfium

    outline = []
    doc = pdfium.PdfDocument(str(path))
    try:
        page_count = len(doc)
        try:
            for item in doc.get_toc():
                outline.append({
                    "level": int(getattr(item, "level", 0)),
                    "title": str(getattr(item, "title", "") or "").strip(),
                    "page": (
                        int(item.page_index) + 1
                        if getattr(item, "page_index", None) is not None
                        else None
                    ),
                })
                if len(outline) >= 100:
                    break
        except Exception:
            pass
    finally:
        doc.close()

    metadata = {}
    pages_info = []
    scanned_pages = []
    with pdfplumber.open(str(path)) as pdf:
        raw_meta = pdf.metadata or {}
        for key in ("Title", "Author", "Subject", "Creator", "Producer"):
            val = raw_meta.get(key)
            if isinstance(val, str) and val.strip():
                metadata[key.lower()] = val.strip()[:200]
        sample_limit = min(len(pdf.pages), 50)
        for idx in range(sample_limit):
            page = pdf.pages[idx]
            text = (page.extract_text() or "").strip()
            img_count = len(page.images or ())
            tables = page.find_tables() or []
            likely_scanned = img_count > 0 and len(text) < 40
            if likely_scanned:
                scanned_pages.append(idx + 1)
            pages_info.append({
                "page": idx + 1,
                "width": round(float(page.width), 1),
                "height": round(float(page.height), 1),
                "chars": len(text),
                "images": img_count,
                "tables": len(tables),
                "likely_scanned": likely_scanned,
            })

    lines = [f"PDF document: {page_count} page(s)."]
    if metadata:
        meta_bits = ", ".join(f"{k}={v}" for k, v in metadata.items())
        lines.append(f"Metadata: {meta_bits}")
    if outline:
        lines.append("Outline:")
        for item in outline[:30]:
            indent = "  " * max(0, item["level"])
            pg = f" (p. {item['page']})" if item["page"] else ""
            lines.append(f"  {indent}- {item['title']}{pg}")
    if scanned_pages:
        shown = ", ".join(str(p) for p in scanned_pages[:20])
        lines.append(
            f"Likely scanned/image-only pages: {shown} "
            "(use mode='render' for visual inspection or mode='ocr' for OCR)."
        )
    lines.append("Pages summary:")
    for info in pages_info[:25]:
        flags = []
        if info["tables"]:
            flags.append(f"{info['tables']} table(s)")
        if info["images"]:
            flags.append(f"{info['images']} image(s)")
        if info["likely_scanned"]:
            flags.append("likely scanned")
        extra = f" [{', '.join(flags)}]" if flags else ""
        lines.append(
            f"  Page {info['page']}: {info['chars']} chars, "
            f"{info['width']}x{info['height']}pt{extra}"
        )
    if page_count > 25:
        lines.append(f"  ... ({page_count - 25} more page(s))")
    text, truncated = _clamp_text("\n".join(lines))
    return {
        "ok": True,
        "kind": "pdf",
        "media_type": "application/pdf",
        "pages": page_count,
        "metadata": metadata,
        "outline": outline[:30],
        "scanned_pages": scanned_pages,
        "text": text,
        "truncated": truncated,
    }


def _pdf_layout_hints(page):
    chars = page.chars or []
    if not chars:
        return ""
    sizes = {}
    for ch in chars:
        size = round(float(ch.get("size", 0)), 1)
        if size > 0:
            sizes[size] = sizes.get(size, 0) + 1
    if not sizes:
        return ""
    top_sizes = sorted(sizes.items(), key=lambda item: -item[1])[:4]
    size_desc = ", ".join(f"{sz}pt ({cnt} chars)" for sz, cnt in top_sizes)
    bbox = page.bbox
    return (
        f"[Layout: page {round(bbox[2] - bbox[0], 1)}x"
        f"{round(bbox[3] - bbox[1], 1)}pt; dominant font sizes: {size_desc}]"
    )


def _read_pdf(path, request, work_dir):
    import pdfplumber

    mode = request.get("mode") or "text"
    pages_spec = request.get("pages")
    dpi = request.get("dpi") or DEFAULT_RENDER_DPI
    out_dir = Path(work_dir) / "out"

    if mode == "render":
        rendered, total = _render_pdf_pages(path, pages_spec, dpi, out_dir, "pdf-page")
        if not rendered:
            return {"ok": False, "error": f"No PDF pages matched {pages_spec!r}."}
        desc = ", ".join(
            f"page {r['page']} ({r['width']}x{r['height']})" for r in rendered
        )
        return {
            "ok": True,
            "kind": "pdf",
            "media_type": "application/pdf",
            "pages": total,
            "rendered": rendered,
            "text": (
                f"Rendered {len(rendered)} of {total} PDF page(s) at "
                f"{dpi} DPI: {desc}."
            ),
        }

    if mode == "ocr":
        rendered, total = _render_pdf_pages(
            path, pages_spec or "1-3", max(dpi, 150), out_dir, "ocr-page",
        )
        if not rendered:
            return {"ok": False, "error": f"No PDF pages matched {pages_spec!r}."}
        sections = []
        for item in rendered:
            ocr_text, err = _run_tesseract(item["path"])
            if err is not None:
                return {"ok": False, "error": err}
            body_txt = ocr_text or "(no text recognized)"
            sections.append(f"## Page {item['page']} (OCR)\n\n{body_txt}")
        body, truncated = _clamp_text("\n\n".join(sections))
        return {
            "ok": True,
            "kind": "pdf",
            "media_type": "application/pdf",
            "pages": total,
            "text": body,
            "truncated": truncated,
        }

    with pdfplumber.open(str(path)) as pdf:
        total = len(pdf.pages)
        chosen = _parse_page_spec(pages_spec, total, max_count=20)
        if not chosen:
            return {
                "ok": False,
                "error": (
                    f"No PDF pages matched {pages_spec!r} "
                    f"(document has {total})."
                ),
            }
        sections = []
        for pno in chosen:
            page = pdf.pages[pno - 1]
            parts = [f"## Page {pno} of {total}"]
            hints = _pdf_layout_hints(page)
            if hints:
                parts.append(hints)
            text = (page.extract_text() or "").strip()
            images = len(page.images or ())
            if text:
                parts.append(text)
            elif images:
                parts.append(
                    f"[Page {pno} has {images} image(s) and no extractable text; "
                    "likely scanned. Use mode='render' or mode='ocr'.]"
                )
            else:
                parts.append("[Blank page]")
            tables = page.extract_tables() or []
            for tidx, tbl in enumerate(tables, 1):
                clean_rows = [
                    ["" if cell is None else str(cell) for cell in row]
                    for row in tbl if row
                ]
                if clean_rows:
                    md_tbl = _render_markdown_table(clean_rows)
                    parts.append(f"### Table {tidx}\n{md_tbl}")
            sections.append("\n\n".join(parts))
        body, truncated = _clamp_text("\n\n".join(sections))
        return {
            "ok": True,
            "kind": "pdf",
            "media_type": "application/pdf",
            "pages": total,
            "text": body,
            "truncated": truncated,
        }


# ------------------------------------------------------------ Spreadsheets


def _format_excel_value(value, number_format=""):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, datetime.datetime):
        if value.hour == 0 and value.minute == 0 and value.second == 0:
            return value.date().isoformat()
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, datetime.time):
        return value.isoformat(timespec="seconds")
    if isinstance(value, float):
        fmt = str(number_format or "")
        if "%" in fmt:
            return f"{value * 100:.2f}%".rstrip("0").rstrip(".") + (
                "" if f"{value * 100:.2f}".endswith("0") is False else ""
            )
        if value.is_integer():
            return str(int(value))
        return f"{value:.6g}"
    return str(value)


def _format_percent(value):
    pct = value * 100.0
    if abs(pct - round(pct)) < 1e-9:
        return f"{int(round(pct))}%"
    return f"{pct:.2f}%".rstrip("0").rstrip(".") + "%"


def _display_cell(formula_cell, cached_val):
    raw = formula_cell.value
    fmt = formula_cell.number_format or ""
    if isinstance(raw, str) and raw.startswith("="):
        if isinstance(cached_val, float) and "%" in fmt:
            cached_str = _format_percent(cached_val)
        else:
            cached_str = _format_excel_value(cached_val, fmt)
        if cached_str == "":
            return f"{raw} [cached: none]"
        return f"{raw} [cached: {cached_str}]"
    if isinstance(raw, float) and "%" in fmt:
        return _format_percent(raw)
    return _format_excel_value(raw, fmt)


def _parse_sheet_range(range_spec, max_row, max_col, offset, limit):
    from openpyxl.utils import column_index_from_string

    r1 = max(1, int(offset or 1))
    c1 = 1
    r2 = min(max_row, r1 + min(int(limit or 50), MAX_SHEET_ROWS) - 1)
    c2 = min(max_col, MAX_SHEET_COLS)
    if range_spec:
        match = re.fullmatch(
            r"\s*([A-Za-z]{1,3})(\d+)\s*(?::\s*([A-Za-z]{1,3})(\d+))?\s*",
            str(range_spec),
        )
        if not match:
            raise ValueError(
                f"invalid spreadsheet range {range_spec!r} (use e.g. A1:F20)"
            )
        c1 = column_index_from_string(match.group(1).upper())
        r1 = int(match.group(2))
        if match.group(3):
            c2 = column_index_from_string(match.group(3).upper())
            r2 = int(match.group(4))
        else:
            c2, r2 = c1, r1
        if r2 < r1 or c2 < c1:
            raise ValueError(f"invalid spreadsheet range {range_spec!r}")
        r2 = min(r2, r1 + MAX_SHEET_ROWS - 1, max(max_row, r1))
        c2 = min(c2, c1 + MAX_SHEET_COLS - 1, max(max_col, c1))
    return r1, r2, c1, c2


def _check_zip_container_bounds(path):
    """Fail fast on zip-bomb Office packages before opening with readers."""
    with zipfile.ZipFile(str(path), "r") as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise ValueError(
                f"package has {len(infos)} entries, exceeding cap of "
                f"{MAX_ARCHIVE_ENTRIES}"
            )
        total = 0
        for info in infos:
            total += info.file_size
            _check_archive_ratio(
                info.file_size, info.compress_size, info.filename,
            )
            if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise ValueError(
                    f"package total uncompressed size ({total:,} bytes) "
                    f"exceeds cap ({MAX_ARCHIVE_UNCOMPRESSED_BYTES:,} bytes)"
                )


def _inspect_xlsx(path):
    import openpyxl

    _check_zip_container_bounds(path)
    wb = openpyxl.load_workbook(str(path), data_only=False)
    try:
        sheets = []
        lines = [f"Excel workbook: {len(wb.sheetnames)} sheet(s)."]
        for name in wb.sheetnames:
            ws = wb[name]
            state = getattr(ws, "sheet_state", "visible")
            merged_count = len(ws.merged_cells.ranges)
            comment_count = 0
            formula_count = 0
            row_cap = min(ws.max_row or 0, 500)
            col_cap = min(ws.max_column or 0, 50)
            for row in ws.iter_rows(
                min_row=1, max_row=row_cap, min_col=1, max_col=col_cap,
            ):
                for cell in row:
                    if cell.comment is not None:
                        comment_count += 1
                    if isinstance(cell.value, str) and cell.value.startswith("="):
                        formula_count += 1
            info = {
                "name": name,
                "state": state,
                "rows": ws.max_row or 0,
                "cols": ws.max_column or 0,
                "merged_cells": merged_count,
                "comments": comment_count,
                "formulas": formula_count,
            }
            sheets.append(info)
            flags = []
            if state != "visible":
                flags.append(state)
            if merged_count:
                flags.append(f"{merged_count} merged")
            if formula_count:
                flags.append(f"{formula_count} formula(s)")
            if comment_count:
                flags.append(f"{comment_count} comment(s)")
            extra = f" [{', '.join(flags)}]" if flags else ""
            lines.append(
                f"  - {name}: {info['rows']} rows x {info['cols']} cols{extra}"
            )
    finally:
        wb.close()
    text, truncated = _clamp_text("\n".join(lines))
    return {
        "ok": True,
        "kind": "xlsx",
        "media_type": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        "sheets": sheets,
        "text": text,
        "truncated": truncated,
    }


def _read_xlsx(path, request, work_dir):
    import openpyxl
    from openpyxl.utils import get_column_letter

    _check_zip_container_bounds(path)
    mode = request.get("mode") or "text"
    if mode == "render":
        pdf_path, err = _convert_office_to_pdf(path, work_dir)
        if err is not None:
            return {"ok": False, "error": err}
        rendered, total = _render_pdf_pages(
            pdf_path, request.get("pages"), request.get("dpi") or DEFAULT_RENDER_DPI,
            Path(work_dir) / "out", "sheet-page",
        )
        desc = ", ".join(
            f"page {r['page']} ({r['width']}x{r['height']})" for r in rendered
        )
        return {
            "ok": True,
            "kind": "xlsx",
            "media_type": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            "pages": total,
            "rendered": rendered,
            "text": f"Rendered {len(rendered)} of {total} spreadsheet page(s): {desc}.",
        }

    wb_f = openpyxl.load_workbook(str(path), data_only=False)
    wb_v = openpyxl.load_workbook(str(path), data_only=True)
    try:
        sheet_name = request.get("sheet") or wb_f.sheetnames[0]
        if sheet_name not in wb_f.sheetnames:
            avail = ", ".join(wb_f.sheetnames)
            return {
                "ok": False,
                "error": f"Unknown sheet {sheet_name!r}. Available sheets: {avail}.",
            }
        ws_f = wb_f[sheet_name]
        ws_v = wb_v[sheet_name]
        max_row = ws_f.max_row or 0
        max_col = ws_f.max_column or 0
        if max_row == 0 or max_col == 0:
            return {
                "ok": True,
                "kind": "xlsx",
                "media_type": (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                ),
                "text": f"Sheet {sheet_name!r} is empty.",
            }
        r1, r2, c1, c2 = _parse_sheet_range(
            request.get("range"), max_row, max_col,
            request.get("offset"), request.get("limit"),
        )
        header = ["Row"] + [get_column_letter(c) for c in range(c1, c2 + 1)]
        table_rows = [header]
        comments = []
        for r in range(r1, r2 + 1):
            row_cells = [str(r)]
            for c in range(c1, c2 + 1):
                cf = ws_f.cell(row=r, column=c)
                cv = ws_v.cell(row=r, column=c).value
                row_cells.append(_display_cell(cf, cv))
                if cf.comment is not None and cf.comment.text:
                    coord = f"{get_column_letter(c)}{r}"
                    author = f" ({cf.comment.author})" if cf.comment.author else ""
                    comments.append(f"{coord}{author}: {cf.comment.text.strip()}")
            table_rows.append(row_cells)

        merged = []
        for mrange in ws_f.merged_cells.ranges:
            if (mrange.max_row >= r1 and mrange.min_row <= r2
                    and mrange.max_col >= c1 and mrange.min_col <= c2):
                merged.append(str(mrange))
        state = getattr(ws_f, "sheet_state", "visible")
        state_note = f" ({state})" if state != "visible" else ""
        parts = [
            f"## Sheet: {sheet_name}{state_note} "
            f"(rows {r1}-{r2} of {max_row}, cols {get_column_letter(c1)}-"
            f"{get_column_letter(c2)} of {get_column_letter(max_col)})",
            _render_markdown_table(table_rows),
        ]
        if merged:
            parts.append("Merged cells in range: " + ", ".join(sorted(merged)))
        if comments:
            parts.append("Comments:\n" + "\n".join(f"  - {c}" for c in comments))
    finally:
        wb_f.close()
        wb_v.close()
    body, truncated = _clamp_text("\n\n".join(parts))
    return {
        "ok": True,
        "kind": "xlsx",
        "media_type": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        "text": body,
        "truncated": truncated,
    }


# ------------------------------------------------------------ OOXML helpers


def _read_zip_xml_bounded(zf, member_name):
    info = zf.getinfo(member_name)
    if info.file_size > MAX_OOXML_XML_BYTES:
        raise ValueError(f"XML member {member_name!r} exceeds size cap")
    if info.compress_size > 0 and (
        info.file_size / info.compress_size > MAX_ARCHIVE_RATIO
    ):
        raise ValueError(f"XML member {member_name!r} exceeds compression ratio cap")
    buf = bytearray()
    with zf.open(info, "r") as stream:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > MAX_OOXML_XML_BYTES:
                raise ValueError(f"XML member {member_name!r} exceeds size cap")
    return ET.fromstring(bytes(buf))


def _read_core_props(zf):
    if "docProps/core.xml" not in zf.namelist():
        return {}
    try:
        root = _read_zip_xml_bounded(zf, "docProps/core.xml")
    except Exception:
        return {}
    meta = {}
    for key, tag in (
        ("title", f"{{{DC_NS}}}title"),
        ("creator", f"{{{DC_NS}}}creator"),
        ("subject", f"{{{DC_NS}}}subject"),
    ):
        elem = root.find(tag)
        if elem is not None and elem.text and elem.text.strip():
            meta[key] = elem.text.strip()[:200]
    return meta


# ------------------------------------------------------------ DOCX reader


def _w_tag(local):
    return f"{{{W_NS}}}{local}"


def _docx_comments_map(zf):
    if "word/comments.xml" not in zf.namelist():
        return {}
    root = _read_zip_xml_bounded(zf, "word/comments.xml")
    found = {}
    for comment in root.findall(_w_tag("comment")):
        cid = comment.attrib.get(_w_tag("id"), "")
        author = comment.attrib.get(_w_tag("author"), "")
        date = comment.attrib.get(_w_tag("date"), "")
        texts = [
            node.text for node in comment.iter(_w_tag("t"))
            if node.text
        ]
        found[cid] = {
            "id": cid,
            "author": author,
            "date": date[:10] if date else "",
            "text": "".join(texts).strip(),
        }
    return found


def _docx_paragraph_text(p_elem, comments_map):
    """Extract paragraph text preserving tracked changes and comment anchors."""
    parts = []
    referenced_comments = []

    def walk(node):
        tag = node.tag
        if tag == _w_tag("ins"):
            author = node.attrib.get(_w_tag("author"), "")
            inner = "".join(n.text or "" for n in node.iter(_w_tag("t")))
            if inner:
                who = f" ({author})" if author else ""
                parts.append(f"[+ins{who}: {inner}]")
            return
        if tag == _w_tag("del"):
            author = node.attrib.get(_w_tag("author"), "")
            inner = "".join(n.text or "" for n in node.iter(_w_tag("delText")))
            if inner:
                who = f" ({author})" if author else ""
                parts.append(f"[-del{who}: {inner}]")
            return
        if tag == _w_tag("t") and node.text:
            parts.append(node.text)
            return
        if tag == _w_tag("tab"):
            parts.append("\t")
            return
        if tag in (_w_tag("br"), _w_tag("cr")):
            parts.append("\n")
            return
        if tag == _w_tag("commentReference"):
            cid = node.attrib.get(_w_tag("id"), "")
            if cid in comments_map and cid not in referenced_comments:
                referenced_comments.append(cid)
            return
        for child in node:
            walk(child)

    walk(p_elem)
    text = "".join(parts).strip()
    if referenced_comments:
        notes = []
        for cid in referenced_comments:
            c = comments_map[cid]
            who = c["author"] or "comment"
            notes.append(f"[comment #{cid} by {who}: {c['text']}]")
        text = f"{text} {' '.join(notes)}".strip()
    return text


def _docx_heading_level(p_elem):
    ppr = p_elem.find(_w_tag("pPr"))
    if ppr is None:
        return None, None
    style_elem = ppr.find(_w_tag("pStyle"))
    style = style_elem.attrib.get(_w_tag("val")) if style_elem is not None else None
    if style:
        m = re.fullmatch(r"Heading\s*(\d+)", style, re.IGNORECASE)
        if m:
            return min(6, max(1, int(m.group(1)))), style
        if style.lower() == "title":
            return 1, style
    outline = ppr.find(_w_tag("outlineLvl"))
    if outline is not None:
        val = outline.attrib.get(_w_tag("val"), "")
        if val.isdigit():
            return min(6, int(val) + 1), style
    return None, style


def _docx_table_markdown(tbl_elem, comments_map, depth=0):
    rows = []
    nested_notes = []
    for tr in tbl_elem.findall(_w_tag("tr")):
        row_cells = []
        for tc in tr.findall(_w_tag("tc")):
            tcpr = tc.find(_w_tag("tcPr"))
            span = 1
            vmerge = None
            if tcpr is not None:
                gs = tcpr.find(_w_tag("gridSpan"))
                if gs is not None and str(gs.attrib.get(_w_tag("val"), "")).isdigit():
                    span = max(1, int(gs.attrib[_w_tag("val")]))
                vm = tcpr.find(_w_tag("vMerge"))
                if vm is not None:
                    vmerge = vm.attrib.get(_w_tag("val"), "continue")
            cell_parts = []
            for child in tc:
                if child.tag == _w_tag("p"):
                    ptxt = _docx_paragraph_text(child, comments_map)
                    if ptxt:
                        cell_parts.append(ptxt)
                elif child.tag == _w_tag("tbl") and depth < 3:
                    nested_md = _docx_table_markdown(child, comments_map, depth + 1)
                    if nested_md:
                        nested_notes.append(nested_md)
                        cell_parts.append("[nested table below]")
            cell_text = "; ".join(cell_parts)
            if vmerge == "continue" and not cell_text:
                cell_text = "↑"
            if span > 1:
                cell_text = f"{cell_text} [colspan={span}]".strip()
            row_cells.append(cell_text)
            for _ in range(span - 1):
                row_cells.append("←")
        if row_cells:
            rows.append(row_cells)
    md = _render_markdown_table(rows)
    if nested_notes:
        extra = "\n\n".join(f"Nested table:\n{item}" for item in nested_notes)
        return f"{md}\n\n{extra}" if md else extra
    return md


def _docx_blocks(zf):
    doc_root = _read_zip_xml_bounded(zf, "word/document.xml")
    comments_map = _docx_comments_map(zf)
    body = doc_root.find(_w_tag("body"))
    blocks = []
    outline = []
    tracked_count = 0
    table_count = 0
    if body is not None:
        tracked_count = len(list(body.iter(_w_tag("ins")))) + len(
            list(body.iter(_w_tag("del")))
        )
        for child in body:
            if child.tag == _w_tag("p"):
                text = _docx_paragraph_text(child, comments_map)
                level, style = _docx_heading_level(child)
                if not text:
                    continue
                if level is not None:
                    outline.append({"level": level, "title": text[:160]})
                    blocks.append(f"{'#' * level} {text}")
                elif style and style.lower() not in ("normal", "bodytext"):
                    blocks.append(f"[{style}] {text}")
                else:
                    blocks.append(text)
            elif child.tag == _w_tag("tbl"):
                table_count += 1
                md = _docx_table_markdown(child, comments_map)
                if md:
                    blocks.append(md)
    headers = []
    footers = []
    for name in sorted(zf.namelist()):
        if re.fullmatch(r"word/header\d*\.xml", name):
            hroot = _read_zip_xml_bounded(zf, name)
            htxt = " ".join(
                _docx_paragraph_text(p, comments_map)
                for p in hroot.iter(_w_tag("p"))
            ).strip()
            if htxt:
                headers.append(htxt)
        elif re.fullmatch(r"word/footer\d*\.xml", name):
            froot = _read_zip_xml_bounded(zf, name)
            ftxt = " ".join(
                _docx_paragraph_text(p, comments_map)
                for p in froot.iter(_w_tag("p"))
            ).strip()
            if ftxt:
                footers.append(ftxt)
    return {
        "blocks": blocks,
        "outline": outline,
        "table_count": table_count,
        "comments": list(comments_map.values()),
        "tracked_changes": tracked_count,
        "headers": headers,
        "footers": footers,
    }


def _inspect_docx(path):
    _check_zip_container_bounds(path)
    with zipfile.ZipFile(str(path), "r") as zf:
        meta = _read_core_props(zf)
        parsed = _docx_blocks(zf)
    lines = [
        f"Word document (.docx): {len(parsed['blocks'])} block(s), "
        f"{parsed['table_count']} table(s), {len(parsed['comments'])} comment(s), "
        f"{parsed['tracked_changes']} tracked change(s)."
    ]
    if meta:
        lines.append("Metadata: " + ", ".join(f"{k}={v}" for k, v in meta.items()))
    if parsed["headers"]:
        lines.append(f"Headers: {len(parsed['headers'])} present")
    if parsed["footers"]:
        lines.append(f"Footers: {len(parsed['footers'])} present")
    if parsed["outline"]:
        lines.append("Outline:")
        for item in parsed["outline"][:40]:
            indent = "  " * max(0, item["level"] - 1)
            lines.append(f"  {indent}- {item['title']}")
    text, truncated = _clamp_text("\n".join(lines))
    return {
        "ok": True,
        "kind": "docx",
        "media_type": (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        "metadata": meta,
        "blocks": len(parsed["blocks"]),
        "tables": parsed["table_count"],
        "comments": len(parsed["comments"]),
        "tracked_changes": parsed["tracked_changes"],
        "text": text,
        "truncated": truncated,
    }


def _read_docx(path, request, work_dir):
    _check_zip_container_bounds(path)
    mode = request.get("mode") or "text"
    if mode == "render":
        pdf_path, err = _convert_office_to_pdf(path, work_dir)
        if err is not None:
            return {"ok": False, "error": err}
        rendered, total = _render_pdf_pages(
            pdf_path, request.get("pages"), request.get("dpi") or DEFAULT_RENDER_DPI,
            Path(work_dir) / "out", "docx-page",
        )
        desc = ", ".join(
            f"page {r['page']} ({r['width']}x{r['height']})" for r in rendered
        )
        return {
            "ok": True,
            "kind": "docx",
            "media_type": (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            "pages": total,
            "rendered": rendered,
            "text": f"Rendered {len(rendered)} of {total} Word page(s): {desc}.",
        }

    with zipfile.ZipFile(str(path), "r") as zf:
        parsed = _docx_blocks(zf)
    blocks = parsed["blocks"]
    start = max(1, int(request.get("offset") or 1))
    count = int(request.get("limit") or len(blocks) or 1)
    sliced = blocks[start - 1 : start - 1 + count]
    parts = []
    if start == 1 and parsed["headers"]:
        parts.append("Headers:\n" + "\n".join(f"  - {h}" for h in parsed["headers"]))
    parts.extend(sliced)
    if start - 1 + len(sliced) >= len(blocks):
        if parsed["footers"]:
            f_lines = "\n".join(f"  - {f}" for f in parsed["footers"])
            parts.append(f"Footers:\n{f_lines}")
        if parsed["comments"]:
            c_lines = [
                f"  - #{c['id']} ({c['author'] or 'unknown'}): {c['text']}"
                for c in parsed["comments"]
            ]
            parts.append("Comments:\n" + "\n".join(c_lines))
    else:
        nxt = start + len(sliced)
        parts.append(
            f"[Blocks {start}-{start + len(sliced) - 1} of {len(blocks)}; "
            f"offset {nxt} goes on.]"
        )
    body, truncated = _clamp_text("\n\n".join(parts))
    return {
        "ok": True,
        "kind": "docx",
        "media_type": (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        "text": body,
        "truncated": truncated,
    }


# ------------------------------------------------------------ PPTX reader


def _a_tag(local):
    return f"{{{A_NS}}}{local}"


def _p_tag(local):
    return f"{{{P_NS}}}{local}"


def _c_tag(local):
    return f"{{{C_NS}}}{local}"


def _parse_rels(zf, rels_path):
    if rels_path not in zf.namelist():
        return {}
    root = _read_zip_xml_bounded(zf, rels_path)
    rels = {}
    for rel in root.findall(f"{{{REL_NS}}}Relationship"):
        rid = rel.attrib.get("Id")
        target = rel.attrib.get("Target")
        rtype = rel.attrib.get("Type", "")
        if rid and target:
            rels[rid] = (target, rtype)
    return rels


def _resolve_ooxml_target(base_member, target):
    if target.startswith("/"):
        return target.lstrip("/")
    base_dir = posixpath.dirname(base_member)
    return posixpath.normpath(posixpath.join(base_dir, target))


def _pptx_slide_paths(zf):
    pres_path = "ppt/presentation.xml"
    if pres_path not in zf.namelist():
        return sorted(
            n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)
        )
    pres = _read_zip_xml_bounded(zf, pres_path)
    rels = _parse_rels(zf, "ppt/_rels/presentation.xml.rels")
    ordered = []
    sld_lst = pres.find(_p_tag("sldIdLst"))
    if sld_lst is not None:
        for sld_id in sld_lst.findall(_p_tag("sldId")):
            rid = sld_id.attrib.get(f"{{{R_NS}}}id")
            if rid and rid in rels:
                target = _resolve_ooxml_target(pres_path, rels[rid][0])
                if target in zf.namelist():
                    ordered.append(target)
    return ordered


def _pptx_text_body(tx_body):
    lines = []
    for p in tx_body.findall(_a_tag("p")):
        text = "".join(t.text or "" for t in p.iter(_a_tag("t"))).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def _pptx_table(tbl_elem):
    rows = []
    for tr in tbl_elem.findall(_a_tag("tr")):
        row = []
        for tc in tr.findall(_a_tag("tc")):
            tx = tc.find(_a_tag("txBody"))
            cell_text = _pptx_text_body(tx) if tx is not None else ""
            if tc.attrib.get("hMerge") == "1" and not cell_text:
                cell_text = "←"
            elif tc.attrib.get("vMerge") == "1" and not cell_text:
                cell_text = "↑"
            row.append(cell_text)
        if row:
            rows.append(row)
    return _render_markdown_table(rows)


def _pptx_chart_text(zf, chart_path):
    if chart_path not in zf.namelist():
        return ""
    root = _read_zip_xml_bounded(zf, chart_path)
    title_elem = root.find(f".//{_c_tag('title')}")
    title = ""
    if title_elem is not None:
        title = " ".join(
            t.text.strip() for t in title_elem.iter(_a_tag("t")) if t.text
        ).strip()
    series_rows = []
    categories = []
    for ser in root.iter(_c_tag("ser")):
        name_elem = ser.find(_c_tag("tx"))
        ser_name = ""
        if name_elem is not None:
            ser_name = " ".join(
                v.text.strip() for v in name_elem.iter(_c_tag("v")) if v.text
            ).strip()
        if not categories:
            cat_elem = ser.find(_c_tag("cat"))
            if cat_elem is not None:
                pts = sorted(
                    cat_elem.iter(_c_tag("pt")),
                    key=lambda pt: int(pt.attrib.get("idx", "0") or 0),
                )
                categories = [
                    (pt.find(_c_tag("v")).text or "").strip()
                    if pt.find(_c_tag("v")) is not None else ""
                    for pt in pts
                ]
        val_elem = ser.find(_c_tag("val"))
        vals = []
        if val_elem is not None:
            pts = sorted(
                val_elem.iter(_c_tag("pt")),
                key=lambda pt: int(pt.attrib.get("idx", "0") or 0),
            )
            vals = [
                (pt.find(_c_tag("v")).text or "").strip()
                if pt.find(_c_tag("v")) is not None else ""
                for pt in pts
            ]
        if ser_name or vals:
            series_rows.append((ser_name or f"Series {len(series_rows) + 1}", vals))
    if not series_rows and not title:
        return ""
    header = ["Category", *[name for name, _ in series_rows]]
    count = max(
        len(categories), max((len(vals) for _, vals in series_rows), default=0),
    )
    rows = [header]
    for idx in range(count):
        cat = categories[idx] if idx < len(categories) else str(idx + 1)
        row = [cat] + [
            vals[idx] if idx < len(vals) else "" for _, vals in series_rows
        ]
        rows.append(row)
    heading = f"Chart: {title}" if title else "Chart"
    return f"### {heading}\n" + _render_markdown_table(rows)


def _pptx_parse_slide(zf, slide_path, number):
    root = _read_zip_xml_bounded(zf, slide_path)
    rels_name = posixpath.join(
        posixpath.dirname(slide_path), "_rels",
        posixpath.basename(slide_path) + ".rels",
    )
    rels = _parse_rels(zf, rels_name)

    title = ""
    texts = []
    tables = []
    chart_rids = []
    grouped_count = 0

    def walk_tree(container, in_group=False):
        nonlocal title, grouped_count
        for child in container:
            if child.tag == _p_tag("grpSp"):
                grouped_count += 1
                walk_tree(child, in_group=True)
            elif child.tag == _p_tag("sp"):
                tx = child.find(_p_tag("txBody"))
                if tx is None:
                    continue
                body = _pptx_text_body(tx)
                if not body:
                    continue
                is_title = False
                nv = child.find(_p_tag("nvSpPr"))
                if nv is not None:
                    ph = nv.find(f".//{_p_tag('ph')}")
                    if ph is not None and ph.attrib.get("type") in (
                        "title", "ctrTitle",
                    ):
                        is_title = True
                if is_title and not title:
                    title = body.splitlines()[0]
                else:
                    prefix = "[grouped] " if in_group else ""
                    texts.append(prefix + body)
            elif child.tag == _p_tag("graphicFrame"):
                for tbl in child.iter(_a_tag("tbl")):
                    md = _pptx_table(tbl)
                    if md:
                        tables.append(md)
                for chart in child.iter(_c_tag("chart")):
                    rid = chart.attrib.get(f"{{{R_NS}}}id")
                    if rid and rid not in chart_rids:
                        chart_rids.append(rid)

    sp_tree = root.find(f".//{_p_tag('cSld')}/{_p_tag('spTree')}")
    if sp_tree is not None:
        walk_tree(sp_tree)

    charts = []
    for rid in chart_rids:
        if rid in rels:
            chart_path = _resolve_ooxml_target(slide_path, rels[rid][0])
            ctext = _pptx_chart_text(zf, chart_path)
            if ctext:
                charts.append(ctext)

    notes = ""
    comments = []
    for target, rtype in rels.values():
        resolved = _resolve_ooxml_target(slide_path, target)
        if resolved not in zf.namelist():
            continue
        if "notesSlide" in rtype:
            nroot = _read_zip_xml_bounded(zf, resolved)
            ntext = "\n".join(
                _pptx_text_body(tx)
                for tx in nroot.iter(_p_tag("txBody"))
                if _pptx_text_body(tx)
            ).strip()
            if ntext:
                notes = ntext
        elif "comments" in rtype:
            croot = _read_zip_xml_bounded(zf, resolved)
            for cm in croot.iter(_p_tag("cm")):
                ct = cm.find(_p_tag("text"))
                if ct is not None and ct.text and ct.text.strip():
                    comments.append(ct.text.strip())

    return {
        "number": number,
        "title": title,
        "texts": texts,
        "tables": tables,
        "charts": charts,
        "notes": notes,
        "comments": comments,
        "grouped_shapes": grouped_count,
    }


def _inspect_pptx(path):
    _check_zip_container_bounds(path)
    with zipfile.ZipFile(str(path), "r") as zf:
        meta = _read_core_props(zf)
        slide_paths = _pptx_slide_paths(zf)
        slides = [
            _pptx_parse_slide(zf, sp, idx)
            for idx, sp in enumerate(slide_paths, 1)
        ]
    lines = [f"PowerPoint presentation (.pptx): {len(slides)} slide(s)."]
    if meta:
        lines.append("Metadata: " + ", ".join(f"{k}={v}" for k, v in meta.items()))
    lines.append("Slide index:")
    for s in slides:
        flags = []
        if s["tables"]:
            flags.append(f"{len(s['tables'])} table(s)")
        if s["charts"]:
            flags.append(f"{len(s['charts'])} chart(s)")
        if s["grouped_shapes"]:
            flags.append(f"{s['grouped_shapes']} group(s)")
        if s["notes"]:
            flags.append("notes")
        if s["comments"]:
            flags.append(f"{len(s['comments'])} comment(s)")
        extra = f" [{', '.join(flags)}]" if flags else ""
        label = s["title"] or "(untitled)"
        lines.append(f"  {s['number']}. {label}{extra}")
    text, truncated = _clamp_text("\n".join(lines))
    return {
        "ok": True,
        "kind": "pptx",
        "media_type": (
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation"
        ),
        "pages": len(slides),
        "metadata": meta,
        "text": text,
        "truncated": truncated,
    }


def _read_pptx(path, request, work_dir):
    _check_zip_container_bounds(path)
    mode = request.get("mode") or "text"
    if mode == "render":
        pdf_path, err = _convert_office_to_pdf(path, work_dir)
        if err is not None:
            return {"ok": False, "error": err}
        rendered, total = _render_pdf_pages(
            pdf_path, request.get("pages"), request.get("dpi") or DEFAULT_RENDER_DPI,
            Path(work_dir) / "out", "slide",
        )
        desc = ", ".join(
            f"slide {r['page']} ({r['width']}x{r['height']})" for r in rendered
        )
        return {
            "ok": True,
            "kind": "pptx",
            "media_type": (
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            ),
            "pages": total,
            "rendered": rendered,
            "text": f"Rendered {len(rendered)} of {total} slide(s): {desc}.",
        }

    with zipfile.ZipFile(str(path), "r") as zf:
        slide_paths = _pptx_slide_paths(zf)
        total = len(slide_paths)
        chosen = _parse_page_spec(request.get("pages"), total, max_count=20)
        if not chosen:
            return {
                "ok": False,
                "error": (
                    f"No slides matched {request.get('pages')!r} "
                    f"(deck has {total})."
                ),
            }
        sections = []
        for snum in chosen:
            s = _pptx_parse_slide(zf, slide_paths[snum - 1], snum)
            heading = (
                f"## Slide {snum}: {s['title']}" if s["title"]
                else f"## Slide {snum}"
            )
            parts = [heading]
            parts.extend(s["texts"])
            parts.extend(s["tables"])
            parts.extend(s["charts"])
            if s["notes"]:
                parts.append(f"Speaker notes:\n{s['notes']}")
            if s["comments"]:
                c_txt = "\n".join(f"  - {c}" for c in s["comments"])
                parts.append(f"Comments:\n{c_txt}")
            sections.append("\n\n".join(parts))
    body, truncated = _clamp_text("\n\n".join(sections))
    return {
        "ok": True,
        "kind": "pptx",
        "media_type": (
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation"
        ),
        "pages": total,
        "text": body,
        "truncated": truncated,
    }


# ------------------------------------------------------------ Images


def _inspect_image(path):
    from PIL import ExifTags, Image

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    with Image.open(str(path)) as img:
        fmt = (img.format or "unknown").upper()
        width, height = img.size
        mode = img.mode
        frames = getattr(img, "n_frames", 1)
        exif_info = {}
        try:
            exif = img.getexif()
            for tag_id, val in exif.items():
                name = ExifTags.TAGS.get(tag_id, str(tag_id))
                if name in (
                    "Orientation", "Make", "Model", "DateTime",
                    "DateTimeOriginal", "Software", "XResolution", "YResolution",
                ):
                    exif_info[name] = str(val)[:100]
        except Exception:
            pass
    mime = f"image/{fmt.lower()}" if fmt != "JPEG" else "image/jpeg"
    lines = [
        f"Image ({fmt}): {width}x{height} pixels, mode {mode}, {frames} frame(s)."
    ]
    if exif_info:
        lines.append(
            "EXIF: " + ", ".join(f"{k}={v}" for k, v in sorted(exif_info.items()))
        )
    return {
        "ok": True,
        "kind": "image",
        "media_type": mime,
        "width": width,
        "height": height,
        "exif": exif_info,
        "text": "\n".join(lines),
    }


def _read_image(path, request, work_dir):
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    mode = request.get("mode") or "render"
    out_dir = Path(work_dir) / "out"
    crop_spec = request.get("crop")
    max_dim = MAX_RENDER_DIM

    with Image.open(str(path)) as raw_img:
        img = ImageOps.exif_transpose(raw_img)
        if crop_spec:
            parts = [p.strip() for p in str(crop_spec).split(",")]
            if len(parts) != 4 or not all(p.isdigit() for p in parts):
                return {
                    "ok": False,
                    "error": "crop must be 'x,y,width,height' of nonnegative integers.",
                }
            x, y, w, h = (int(p) for p in parts)
            if w <= 0 or h <= 0 or x >= img.width or y >= img.height:
                return {"ok": False, "error": "crop box is outside image bounds."}
            img = img.crop((x, y, min(img.width, x + w), min(img.height, y + h)))
        if max(img.size) > max_dim:
            img.thumbnail((max_dim, max_dim))
        if img.mode not in ("RGB", "RGBA", "L"):
            img = img.convert("RGB")
        out_file = out_dir / "image.png"
        img.save(out_file, format="PNG")
        width, height = img.size

    if mode == "ocr":
        ocr_text, err = _run_tesseract(out_file)
        if err is not None:
            return {"ok": False, "error": err}
        body, truncated = _clamp_text(ocr_text or "(no text recognized)")
        return {
            "ok": True,
            "kind": "image",
            "media_type": "image/png",
            "width": width,
            "height": height,
            "text": body,
            "truncated": truncated,
        }

    return {
        "ok": True,
        "kind": "image",
        "media_type": "image/png",
        "width": width,
        "height": height,
        "rendered": [{
            "page": 1,
            "path": str(out_file),
            "name": out_file.name,
            "width": width,
            "height": height,
        }],
        "text": f"Rendered PNG image ({width}x{height} pixels).",
    }


# ------------------------------------------------------------ Audio / Video


def _probe_ffprobe(path):
    binary = shutil.which("ffprobe")
    if not binary:
        return None
    try:
        done = subprocess.run(
            [
                binary, "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or not done.stdout.strip():
        return None
    try:
        return json.loads(done.stdout)
    except ValueError:
        return None


def _inspect_media(path, kind, mime):
    import tinytag

    info = {}
    try:
        tag = tinytag.TinyTag.get(str(path))
        if tag.duration is not None:
            info["duration_seconds"] = round(float(tag.duration), 3)
        if tag.samplerate:
            info["sample_rate"] = int(tag.samplerate)
        if tag.channels:
            info["channels"] = int(tag.channels)
        if tag.bitrate:
            info["bitrate_kbps"] = round(float(tag.bitrate), 1)
        tags = {}
        for field in ("title", "artist", "album", "track", "year", "genre", "comment"):
            val = getattr(tag, field, None)
            if val:
                tags[field] = str(val).strip()[:160]
        if tags:
            info["tags"] = tags
    except Exception:
        pass

    ff = _probe_ffprobe(path)
    streams_summary = []
    if isinstance(ff, dict):
        fmt = ff.get("format") or {}
        if "duration_seconds" not in info and fmt.get("duration"):
            try:
                info["duration_seconds"] = round(float(fmt["duration"]), 3)
            except ValueError:
                pass
        if fmt.get("format_name"):
            info["container"] = str(fmt["format_name"])
        for stream in ff.get("streams") or ():
            if not isinstance(stream, dict):
                continue
            ctype = stream.get("codec_type", "")
            cname = stream.get("codec_name", "")
            if ctype == "video":
                w = stream.get("width")
                h = stream.get("height")
                fps = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
                if w and h:
                    info["width"] = int(w)
                    info["height"] = int(h)
                fps_txt = f" @ {fps} fps" if fps and fps != "0/0" else ""
                res = f" {w}x{h}" if w and h else ""
                streams_summary.append(f"video: {cname}{res}{fps_txt}")
            elif ctype == "audio":
                sr = stream.get("sample_rate")
                ch = stream.get("channels")
                if sr and "sample_rate" not in info:
                    info["sample_rate"] = int(sr)
                if ch and "channels" not in info:
                    info["channels"] = int(ch)
                streams_summary.append(
                    f"audio: {cname} {sr or '?'}Hz {ch or '?'}ch"
                )

    label = "Video" if kind == "video" else "Audio"
    bits = []
    if "duration_seconds" in info:
        bits.append(f"duration {info['duration_seconds']}s")
    if "width" in info and "height" in info:
        bits.append(f"{info['width']}x{info['height']}")
    if "sample_rate" in info:
        bits.append(f"{info['sample_rate']} Hz")
    if "channels" in info:
        bits.append(f"{info['channels']} ch")
    if "bitrate_kbps" in info:
        bits.append(f"{info['bitrate_kbps']} kbps")
    detail = ", ".join(bits) or "metadata unavailable"
    lines = [f"{label} file ({mime}): {detail}."]
    if streams_summary:
        lines.append("Streams: " + "; ".join(streams_summary))
    if info.get("tags"):
        lines.append(
            "Tags: " + ", ".join(f"{k}={v}" for k, v in info["tags"].items())
        )
    if ff is None:
        lines.append("(ffprobe not installed; detailed stream/codec info omitted.)")
    return {
        "ok": True,
        "kind": kind,
        "media_type": mime,
        **{
            k: info[k]
            for k in ("duration_seconds", "width", "height", "sample_rate", "channels")
            if k in info
        },
        "text": "\n".join(lines),
    }


# ------------------------------------------------------------ Archives


def _is_safe_archive_member(name):
    if not name or "\0" in name or "\\" in name:
        return False
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return False
    parts = posixpath.normpath(name).split("/")
    if not parts or parts[0] == ".." or ".." in parts:
        return False
    raw_parts = name.split("/")
    if ".." in raw_parts:
        return False
    return True


def _check_archive_ratio(uncompressed, compressed, member_label):
    if uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        raise ValueError(
            "archive exceeds uncompressed cap "
            f"({MAX_ARCHIVE_UNCOMPRESSED_BYTES:,} bytes)"
        )
    if compressed > 0 and (uncompressed / compressed) > MAX_ARCHIVE_RATIO:
        raise ValueError(
            f"archive member {member_label!r} exceeds compression ratio cap "
            f"({MAX_ARCHIVE_RATIO:g}x)"
        )


def _read_bounded_stream(stream, *, max_bytes, compressed_size=0, label="member"):
    buf = bytearray()
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise ValueError(
                f"archive member {label!r} exceeds {max_bytes:,} byte cap"
            )
        if compressed_size > 0 and (len(buf) / compressed_size) > MAX_ARCHIVE_RATIO:
            raise ValueError(
                f"archive member {label!r} exceeds compression ratio cap "
                f"({MAX_ARCHIVE_RATIO:g}x)"
            )
    return bytes(buf)


def _inspect_archive(path):
    lower = str(path).lower()
    if lower.endswith(".zip") or zipfile.is_zipfile(str(path)):
        entries = []
        rejected = []
        total_uncompressed = 0
        with zipfile.ZipFile(str(path), "r") as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                return {
                    "ok": False,
                    "error": (
                        f"Archive rejected: {len(infos)} entries exceeds "
                        f"limit of {MAX_ARCHIVE_ENTRIES}."
                    ),
                }
            for info in infos:
                if not _is_safe_archive_member(info.filename):
                    rejected.append(info.filename)
                    continue
                total_uncompressed += info.file_size
                _check_archive_ratio(
                    info.file_size, info.compress_size, info.filename,
                )
                if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise ValueError(
                        f"archive total uncompressed size "
                        f"({total_uncompressed:,} bytes) "
                        f"exceeds cap ({MAX_ARCHIVE_UNCOMPRESSED_BYTES:,} bytes)"
                    )
                entries.append({
                    "name": info.filename,
                    "size": info.file_size,
                    "compressed": info.compress_size,
                    "is_dir": info.is_dir(),
                })
        lines = [
            f"ZIP archive: {len(entries)} safe member(s), "
            f"{total_uncompressed:,} uncompressed bytes."
        ]
        if rejected:
            lines.append(
                f"Rejected unsafe path-traversal member(s): {', '.join(rejected[:10])}"
            )
        for item in entries[:100]:
            kind_mark = "/" if item["is_dir"] else f" ({item['size']:,} bytes)"
            lines.append(f"  - {item['name']}{kind_mark}")
        if len(entries) > 100:
            lines.append(f"  ... ({len(entries) - 100} more)")
        text, truncated = _clamp_text("\n".join(lines))
        return {
            "ok": True,
            "kind": "archive",
            "media_type": "application/zip",
            "member_count": len(entries),
            "rejected_members": rejected,
            "text": text,
            "truncated": truncated,
        }

    if tarfile.is_tarfile(str(path)):
        entries = []
        rejected = []
        total_uncompressed = 0
        archive_size = os.path.getsize(path)
        with tarfile.open(str(path), "r:*") as tf:
            for member in tf:
                if len(entries) + len(rejected) >= MAX_ARCHIVE_ENTRIES:
                    return {
                        "ok": False,
                        "error": (
                            f"Archive rejected: exceeds limit of "
                            f"{MAX_ARCHIVE_ENTRIES} entries."
                        ),
                    }
                if not _is_safe_archive_member(member.name):
                    rejected.append(member.name)
                    continue
                if member.issym() or member.islnk():
                    target = member.linkname
                    if not _is_safe_archive_member(target):
                        rejected.append(f"{member.name} -> {target}")
                        continue
                total_uncompressed += max(0, member.size)
                _check_archive_ratio(
                    total_uncompressed, archive_size, member.name,
                )
                entries.append({
                    "name": member.name,
                    "size": member.size,
                    "is_dir": member.isdir(),
                })
        lines = [
            f"TAR archive: {len(entries)} safe member(s), "
            f"{total_uncompressed:,} uncompressed bytes."
        ]
        if rejected:
            lines.append(
                f"Rejected unsafe member(s): {', '.join(rejected[:10])}"
            )
        for item in entries[:100]:
            kind_mark = "/" if item["is_dir"] else f" ({item['size']:,} bytes)"
            lines.append(f"  - {item['name']}{kind_mark}")
        text, truncated = _clamp_text("\n".join(lines))
        return {
            "ok": True,
            "kind": "archive",
            "media_type": "application/x-tar",
            "member_count": len(entries),
            "rejected_members": rejected,
            "text": text,
            "truncated": truncated,
        }

    if lower.endswith(".gz"):
        comp_size = os.path.getsize(path)
        with gzip.open(str(path), "rb") as gz:
            data = _read_bounded_stream(
                gz, max_bytes=MAX_ARCHIVE_MEMBER_BYTES,
                compressed_size=comp_size, label=os.path.basename(path),
            )
        return {
            "ok": True,
            "kind": "archive",
            "media_type": "application/gzip",
            "text": (
                f"Gzip file: {comp_size:,} compressed bytes -> "
                f"{len(data):,} uncompressed bytes."
            ),
        }

    return {"ok": False, "error": "Unsupported or corrupt archive format."}


def _read_archive(path, request):
    member_name = request.get("member")
    lower = str(path).lower()
    if lower.endswith(".gz") and not tarfile.is_tarfile(str(path)):
        comp_size = os.path.getsize(path)
        with gzip.open(str(path), "rb") as gz:
            raw = _read_bounded_stream(
                gz, max_bytes=MAX_ARCHIVE_MEMBER_BYTES,
                compressed_size=comp_size, label=os.path.basename(path),
            )
        return _format_member_bytes(os.path.basename(path), raw, request)

    if not member_name:
        return _inspect_archive(path)
    if not _is_safe_archive_member(member_name):
        return {
            "ok": False,
            "error": f"Refused unsafe archive member path {member_name!r}.",
        }

    if lower.endswith(".zip") or zipfile.is_zipfile(str(path)):
        with zipfile.ZipFile(str(path), "r") as zf:
            try:
                info = zf.getinfo(member_name)
            except KeyError:
                return {
                    "ok": False,
                    "error": f"Archive member {member_name!r} not found.",
                }
            if info.is_dir():
                return {
                    "ok": False,
                    "error": f"Archive member {member_name!r} is a directory.",
                }
            _check_archive_ratio(info.file_size, info.compress_size, member_name)
            with zf.open(info, "r") as stream:
                raw = _read_bounded_stream(
                    stream, max_bytes=MAX_ARCHIVE_MEMBER_BYTES,
                    compressed_size=info.compress_size, label=member_name,
                )
        return _format_member_bytes(member_name, raw, request)

    if tarfile.is_tarfile(str(path)):
        archive_size = os.path.getsize(path)
        with tarfile.open(str(path), "r:*") as tf:
            try:
                member = tf.getmember(member_name)
            except KeyError:
                return {
                    "ok": False,
                    "error": f"Archive member {member_name!r} not found.",
                }
            if not member.isfile():
                return {
                    "ok": False,
                    "error": f"Archive member {member_name!r} is not a regular file.",
                }
            _check_archive_ratio(
                max(0, member.size), archive_size, member_name,
            )
            stream = tf.extractfile(member)
            if stream is None:
                return {
                    "ok": False,
                    "error": f"Could not read archive member {member_name!r}.",
                }
            with stream:
                raw = _read_bounded_stream(
                    stream,
                    max_bytes=MAX_ARCHIVE_MEMBER_BYTES,
                    compressed_size=archive_size,
                    label=member_name,
                )
        return _format_member_bytes(member_name, raw, request)

    return {"ok": False, "error": "Unsupported or corrupt archive format."}


def _format_member_bytes(name, raw, request):
    if b"\0" in raw[:BINARY_PROBE_BYTES]:
        return {
            "ok": True,
            "kind": "archive",
            "media_type": "application/octet-stream",
            "text": f"Archive member {name}: binary ({len(raw):,} bytes).",
        }
    lines = raw.decode("utf-8", errors="replace").splitlines()
    start = max(1, int(request.get("offset") or 1))
    limit = request.get("limit")
    if start > max(1, len(lines)):
        return {
            "ok": False,
            "error": (
                f"Member {name} has {len(lines)} lines; "
                f"offset {start} is past end."
            ),
        }
    end = len(lines) if limit is None else min(len(lines), start + int(limit) - 1)
    numbered = "\n".join(f"{n}→{lines[n - 1]}" for n in range(start, end + 1))
    if end < len(lines):
        numbered += (
            f"\n\n[Lines {start}–{end} of {len(lines)}; "
            f"offset {end + 1} goes on.]"
        )
    body, truncated = _clamp_text(numbered)
    return {
        "ok": True,
        "kind": "archive",
        "media_type": "text/plain",
        "text": body,
        "truncated": truncated,
    }


# ------------------------------------------------------------ Text fallback


def _inspect_text(path, mime):
    size = os.path.getsize(path)
    with open(path, "rb") as stream:
        raw = stream.read(MAX_TEXT_INSPECT_BYTES)
    decoded = raw.decode("utf-8", errors="replace")
    lines = decoded.splitlines()
    partial = size > MAX_TEXT_INSPECT_BYTES
    note = f" (first {MAX_TEXT_INSPECT_BYTES:,} bytes inspected)" if partial else ""
    out = [f"Text file ({mime}): {size:,} bytes, {len(lines):,} line(s){note}."]
    if mime == "text/csv" and lines:
        try:
            cols = next(csv.reader(io.StringIO(lines[0])))
            out.append("CSV columns: " + ", ".join(cols[:30]))
        except Exception:
            pass
    elif mime == "application/json" and not partial:
        try:
            parsed = json.loads(decoded)
            if isinstance(parsed, dict):
                out.append("JSON keys: " + ", ".join(list(parsed.keys())[:30]))
            elif isinstance(parsed, list):
                out.append(f"JSON array: {len(parsed)} item(s)")
        except ValueError:
            pass
    return {
        "ok": True,
        "kind": "text",
        "media_type": mime,
        "text": "\n".join(out),
    }


def _read_text(path, mime, request):
    with open(path, "rb") as stream:
        raw = stream.read(MAX_TEXT_INSPECT_BYTES)
    lines = raw.decode("utf-8", errors="replace").splitlines()
    start = max(1, int(request.get("offset") or 1))
    limit = int(request.get("limit") or 200)
    if not lines:
        return {"ok": True, "kind": "text", "media_type": mime, "text": "(empty file)"}
    if start > len(lines):
        return {
            "ok": False,
            "error": f"File has {len(lines)} lines; offset {start} is past end.",
        }
    end = min(len(lines), start + limit - 1)
    body = "\n".join(f"{n}→{lines[n - 1]}" for n in range(start, end + 1))
    if end < len(lines):
        body += f"\n\n[Lines {start}–{end} of {len(lines)}; offset {end + 1} goes on.]"
    body, truncated = _clamp_text(body)
    return {
        "ok": True,
        "kind": "text",
        "media_type": mime,
        "text": body,
        "truncated": truncated,
    }


# ------------------------------------------------------------ Main dispatch


def _stage_file(resolved, request, work_dir):
    """Stream-copy `resolved` into private `work_dir/out/staged` inside bwrap."""
    max_bytes = request.get("max_bytes")
    out_dir = Path(work_dir) / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / "staged"
    hasher = hashlib.sha256()
    head = bytearray()
    total = 0
    with open(resolved, "rb") as src, open(dest, "wb") as dst:
        while chunk := src.read(65536):
            total += len(chunk)
            if max_bytes is not None and total > int(max_bytes):
                dst.close()
                dest.unlink(missing_ok=True)
                return {
                    "ok": False,
                    "kind": "too_big",
                    "path": resolved,
                    "size": total,
                    "error": f"file exceeds staging cap ({int(max_bytes):,} bytes)",
                }
            if len(head) < 4096:
                head.extend(chunk[: 4096 - len(head)])
            hasher.update(chunk)
            dst.write(chunk)
    return {
        "ok": True,
        "kind": "file",
        "path": resolved,
        "staged_path": str(dest),
        "size": total,
        "sha256": hasher.hexdigest(),
        "head": base64.b64encode(bytes(head)).decode("ascii"),
    }


def handle_request(request):
    op = request.get("op")
    raw_path = request.get("path")
    work_dir = request.get("work_dir") or tempfile.gettempdir()
    if not isinstance(raw_path, str) or not raw_path:
        return {"ok": False, "error": "missing file path"}
    resolved = os.path.realpath(os.path.abspath(os.path.expanduser(raw_path)))
    if not os.path.lexists(resolved):
        return {"ok": False, "kind": "missing", "path": resolved,
                "error": "file does not exist", "errno": 2}
    if os.path.isdir(resolved):
        return {"ok": False, "kind": "directory", "path": resolved,
                "error": "path is a directory"}
    if not os.path.isfile(resolved):
        return {"ok": False, "kind": "other", "path": resolved,
                "error": "path is not a regular file"}
    if op == "stage":
        return _stage_file(resolved, request, work_dir)
    size_bytes = os.path.getsize(resolved)
    with open(resolved, "rb") as stream:
        head = stream.read(BINARY_PROBE_BYTES)
    kind, mime = _detect_kind(resolved, head)

    if kind == "heif":
        return {
            "ok": False,
            "kind": "image",
            "media_type": mime,
            "size_bytes": size_bytes,
            "error": (
                "HEIC/HEIF images are not supported by the bundled MIT image reader; "
                "convert to PNG or JPEG first."
            ),
        }

    if op == "inspect":
        try:
            if kind == "pdf":
                result = _inspect_pdf(resolved)
            elif kind == "xlsx":
                result = _inspect_xlsx(resolved)
            elif kind == "docx":
                result = _inspect_docx(resolved)
            elif kind == "pptx":
                result = _inspect_pptx(resolved)
            elif kind == "image":
                result = _inspect_image(resolved)
            elif kind in ("audio", "video"):
                result = _inspect_media(resolved, kind, mime)
            elif kind == "archive":
                result = _inspect_archive(resolved)
            elif kind == "text":
                result = _inspect_text(resolved, mime)
            else:
                result = {
                    "ok": True,
                    "kind": "binary",
                    "media_type": mime,
                    "text": f"Binary file ({mime}): {size_bytes:,} bytes.",
                }
        except ValueError as exc:
            result = {"ok": False, "error": str(exc)}
        result.setdefault("size_bytes", size_bytes)
        result.setdefault("media_type", mime)
        result.setdefault("kind", kind)
        return result

    if op == "read_part":
        try:
            if kind == "pdf":
                result = _read_pdf(resolved, request, work_dir)
            elif kind == "xlsx":
                result = _read_xlsx(resolved, request, work_dir)
            elif kind == "docx":
                result = _read_docx(resolved, request, work_dir)
            elif kind == "pptx":
                result = _read_pptx(resolved, request, work_dir)
            elif kind == "image":
                result = _read_image(resolved, request, work_dir)
            elif kind in ("audio", "video"):
                result = _inspect_media(resolved, kind, mime)
            elif kind == "archive":
                result = _read_archive(resolved, request)
            elif kind == "text":
                result = _read_text(resolved, mime, request)
            else:
                result = {
                    "ok": False,
                    "kind": "binary",
                    "media_type": mime,
                    "error": (
                        f"Binary file ({mime}, {size_bytes:,} bytes) has no "
                        "structured reader."
                    ),
                }
        except ValueError as exc:
            result = {"ok": False, "error": str(exc)}
        result.setdefault("size_bytes", size_bytes)
        result.setdefault("media_type", mime)
        result.setdefault("kind", kind)
        return result

    return {"ok": False, "error": f"unknown reader op {op!r}"}


def main():
    try:
        request = json.load(sys.stdin)
        _apply_resource_limits(
            render_mode=bool(request.get("render_mode")),
            stage_mode=(request.get("op") == "stage"),
        )
        answer = handle_request(request)
    except OSError as exc:
        answer = {"ok": False, "error": exc.strerror or str(exc), "errno": exc.errno}
    except Exception as exc:
        answer = {"ok": False, "error": str(exc)}
    json.dump(answer, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
