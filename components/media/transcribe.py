"""Speech-to-text tool and Same-Provider Default STT service selection.

Works on either a session `attachment_id` or a workspace/local audio `path` in
place, validates path visibility through the session's sandbox, resolves the
STT service strictly under Same-Provider Default (live `stt_service` + live
`session.model.server.name` + keyless local fallback — never silently billing
another paid provider's key), and returns raw verbatim STT text with no extra
LLM rewrite pass.
"""

import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from components.attachments import metadata as attachment_metadata
from components.attachments.store import short_id_of
from components.model import SERVICE_CURRENT_SERVER, SERVICE_OFF, servers
from components.model.apis import discover_utility_services, select_utility_service
from components.model.apis.stt import LocalWhisper, STTError
from components.model.servers import ModelServerError
from components.sandbox.sandbox import (
    ISOLATION,
    POINTERS,
    SandboxError,
    harness_free_env,
    tmp_socket_holders,
)
from core.control import Stopped
from core.tools import Result, tool

DEFAULT_STT_TIMEOUT_SECONDS = 120
MAX_AUDIO_STAGE_BYTES = 64 * 1024 * 1024
PYTHON = os.path.realpath(sys.executable)
STAGE_SNIPPET = (
    "import json, os, sys; "
    "src, dst, cap = sys.argv[1], sys.argv[2], int(sys.argv[3]); "
    "p = os.path.abspath(os.path.expanduser(src)); "
    "r = os.path.realpath(p) if os.path.lexists(p) else p; "
    "k = ('missing' if not os.path.lexists(r) else "
    "'directory' if os.path.isdir(r) else "
    "'file' if os.path.isfile(r) else 'other'); "
    "out = {'path': r, 'kind': k}; "
    "total = 0\n"
    "if k == 'file':\n"
    "    with open(r, 'rb') as fi, open(dst, 'wb') as fo:\n"
    "        while chunk := fi.read(65536):\n"
    "            total += len(chunk)\n"
    "            if total > cap:\n"
    "                out['kind'] = 'too_big'; break\n"
    "            fo.write(chunk)\n"
    "    out['size'] = total\n"
    "print(json.dumps(out))"
)


class Media:
    """Live session carrier for media tools (tracks model and setting switches)."""

    def __init__(self, transport=None, *, model_server=None):
        self.transport = transport
        self.model_server = model_server
        self._session = None

    def bind_session(self, session):
        """Attach the live session so model switches and settings stay current."""
        self._session = session

    def active_model_server(self, session=None):
        """Explicit server pin if given, else live `session.model.server.name`."""
        if self.model_server is not None:
            return self.model_server
        live = session or self._session
        if live is None:
            return None
        model = getattr(live, "model", None)
        server = getattr(model, "server", None)
        return getattr(server, "name", None)


def available_stt_services():
    """Eligible STT services without probing the network."""
    return servers.utility_services(
        "stt", discover_utility_services, extra_key=LocalWhisper.binary(),
    )


def eligible_stt_services(services, model_server, saved):
    """Subset of STT services authorized under Same-Provider Default."""
    setting = SERVICE_CURRENT_SERVER if saved is None else saved
    if setting == SERVICE_OFF:
        return {}
    if setting != SERVICE_CURRENT_SERVER:
        chosen = services.get(setting)
        return {setting: chosen} if chosen is not None else {}
    return {
        name: svc for name, svc in services.items()
        if not svc.key or name == model_server
    }


def select_stt_service(services, model_server, saved, requested=None):
    """Resolve one STT service strictly under Same-Provider Default."""
    return select_utility_service(
        services, model_server, saved, requested,
        label="Speech-to-text service",
    )


def active_stt_setting(limits, media=None, session=None):
    """Authoritative `stt_service`: live session.settings first, else limits."""
    live = session or (media._session if media is not None else None)
    if live is not None:
        session_settings = getattr(live, "settings", None)
        if isinstance(session_settings, dict) and "stt_service" in session_settings:
            val = session_settings["stt_service"]
            return SERVICE_CURRENT_SERVER if val is None else val
    if limits is not None and "stt_service" in limits:
        val = limits["stt_service"]
        return SERVICE_CURRENT_SERVER if val is None else val
    return SERVICE_CURRENT_SERVER


def _validate_audio_source(args):
    has_path = isinstance(args.get("path"), str) and bool(args["path"].strip())
    has_att = (
        isinstance(args.get("attachment_id"), str)
        and bool(args["attachment_id"].strip())
    )
    if has_path == has_att:
        return "provide exactly one of `path` or `attachment_id`"
    return None


def _resolve_audio_attachment(
    attachment_id, session_attachments=None, attachments_dir=None,
):
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
                raw_head = target.read_bytes()[:4096]
                mime = attachment_metadata.media_type(target.name, raw_head)
                probed = attachment_metadata.probe(target, mime)
                return {
                    "attachment_id": attachment_id,
                    "name": target.name.removeprefix(prefix) or target.name,
                    "media_type": mime,
                    "size_bytes": target.stat().st_size,
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "path": str(target),
                    "metadata": probed,
                }
    return None


def _stage_bwrap_command(box, raw_path, dest_file, work_dir):
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise SandboxError("bwrap is not installed, so nothing can run")
    workspace = Path(box.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    ws = str(workspace)
    args = [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    if hasattr(box, "covered_folders"):
        for folder in box.covered_folders():
            if Path(folder).exists():
                args += ["--tmpfs", str(folder)]
    for holder in tmp_socket_holders():
        if holder.is_dir():
            args += ["--tmpfs", str(holder)]
        elif os.path.lexists(holder):
            args += ["--ro-bind", "/dev/null", str(holder)]
    if workspace != Path("/"):
        args += ["--ro-bind", ws, ws]
    if hasattr(box, "private_tmp"):
        session_tmp = box.private_tmp()
        if session_tmp and Path(session_tmp).exists():
            args += ["--ro-bind", str(session_tmp), str(session_tmp)]
    strict_box = box.without_changes() if hasattr(box, "without_changes") else None
    if strict_box is not None and hasattr(strict_box, "hiding"):
        args += strict_box.hiding()
    elif hasattr(box, "hiding"):
        args += box.hiding()
    if hasattr(box, "shown_again"):
        args += box.shown_again()
    tmpfs_mounts = [
        args[i + 1] for i, val in enumerate(args) if val == "--tmpfs"
    ]
    for mount in tmpfs_mounts:
        args += ["--remount-ro", mount]
    args += ["--bind", str(work_dir), str(work_dir)]
    args += [*ISOLATION, "--disable-userns", "--chdir", ws, "--"]
    args += [
        PYTHON, "-I", "-c", STAGE_SNIPPET,
        raw_path, str(dest_file), str(MAX_AUDIO_STAGE_BYTES),
    ]
    env = harness_free_env()
    for name in POINTERS:
        env.pop(name, None)
    env["TMPDIR"] = str(work_dir)
    return args, env


def _stage_sandbox_audio_path(box, raw_path, stage_dir):
    """Stage `raw_path` from inside single-layer bwrap to avoid symlink TOCTOU.

    Returns `(shown_path, local_read_path, error)`.
    """
    if box is None or not getattr(box, "sandboxed", False):
        base = Path(box.workspace).resolve() if box is not None else Path.cwd()
        candidate = Path(os.path.expanduser(raw_path))
        if not candidate.is_absolute():
            candidate = base / candidate
        resolved = Path(os.path.realpath(candidate))
        if not resolved.exists():
            return None, None, f"{raw_path} doesn't exist."
        if resolved.is_dir():
            return None, None, f"{raw_path} is a directory."
        if not resolved.is_file():
            return None, None, f"{raw_path} isn't a regular file."
        return str(resolved), str(resolved), None

    safe_name = Path(raw_path).name or "audio"
    dest_file = Path(stage_dir) / safe_name
    tries = 0
    while True:
        tries += 1
        try:
            cmd, env = _stage_bwrap_command(box, raw_path, dest_file, stage_dir)
            done = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                cwd=box.workspace,
                timeout=30,
            )
        except (SandboxError, OSError, subprocess.SubprocessError) as exc:
            return None, None, f"could not access {raw_path}: {exc}"
        if not (hasattr(box, "start_again")
                and box.start_again(tries, done.returncode, done.stderr)):
            break
    try:
        info = json.loads(done.stdout)
    except ValueError:
        tail = done.stderr.strip()[-200:] or f"exit code {done.returncode}"
        return None, None, f"could not access {raw_path}: {tail}"
    kind = info.get("kind")
    shown = info.get("path") or raw_path
    if kind == "missing":
        return None, None, f"{shown} doesn't exist."
    if kind == "directory":
        return None, None, f"{shown} is a directory."
    if kind == "too_big":
        limit_mb = MAX_AUDIO_STAGE_BYTES // (1024 * 1024)
        return None, None, f"{shown} exceeds the {limit_mb} MiB audio staging cap."
    if kind != "file" or not dest_file.is_file():
        return None, None, f"{shown} isn't a regular file."
    return shown, str(dest_file), None


def _run_cancellable(work, stop, seconds):
    done = queue.Queue()

    def runner():
        try:
            done.put((True, work()))
        except BaseException as exc:
            done.put((False, exc))

    threading.Thread(target=runner, daemon=True).start()
    deadline = time.monotonic() + seconds + 5
    while True:
        if stop is not None and stop.is_set():
            raise Stopped()
        try:
            ok, val = done.get(timeout=0.1)
        except queue.Empty:
            if time.monotonic() > deadline:
                raise STTError(
                    f"Transcription did not finish within {seconds:g} s."
                ) from None
            continue
        if ok:
            return val
        raise val


def _describe_stt_service(limits, context):
    text = (
        "Optional speech-to-text service name for this transcription. "
        "Omit to use the current session choice."
    )
    media = context.get("media")
    if media is None:
        return text
    session = context.get("media_session")
    saved = active_stt_setting(limits, media, session)
    if saved == SERVICE_OFF:
        return "Speech-to-text is off in this session's settings."
    try:
        services = available_stt_services()
    except ModelServerError:
        return text
    model_server = media.active_model_server(session)
    chosen, _ = select_stt_service(services, model_server, saved)
    allowed = eligible_stt_services(services, model_server, saved)
    if chosen is not None:
        text += f" The current choice is {chosen.name}."
    if allowed:
        text += f" Available services are {', '.join(allowed)}."
    return text


def _transcribe_summary(args):
    target = (
        f"attachment {args['attachment_id']}"
        if args.get("attachment_id")
        else args.get("path", "")
    )
    svc = args.get("service")
    return f"transcribe_audio {target}" + (f" via {svc}" if svc else "")


TRANSCRIBE_DESCRIPTION = (
    "Transcribe audio (`path` or `attachment_id`, in place) into raw text "
    "with no rewrite pass. Uses `stt_service` under Same-Provider Default "
    "(active model server or local fallback only)."
)


@tool(
    TRANSCRIBE_DESCRIPTION,
    optional=("path", "attachment_id", "language", "service"),
    validate=_validate_audio_source,
    summary=_transcribe_summary,
    traits=("side_by_side",),
    metadata={"permissions": {"needs_permission": lambda args: False}},
    path=("string", "Audio file path (in place)"),
    attachment_id=("string", "Audio attachment ID"),
    language=("string", "Optional ISO-639-1 language hint"),
    service=("string", _describe_stt_service),
    fields={
        "service": ("string", "STT service used"),
        "model": ("string", "STT model or binary used"),
        "attachment_id": ("string", "Source attachment ID"),
        "path": ("string", "Source path"),
        "media_type": ("string", "Audio MIME type"),
        "language": ("string", "Language"),
        "duration_seconds": ("number", "Duration in seconds"),
    },
)
def transcribe_audio(
    path=None,
    attachment_id=None,
    language=None,
    service=None,
    sandbox=None,
    limits=None,
    media=None,
    media_session=None,
    session_attachments=None,
    attachments_dir=None,
    stop=None,
    notes=None,
):
    """Transcribe an audio attachment or workspace path verbatim."""
    provenance = {}
    stage_dir = None
    try:
        if attachment_id:
            att = _resolve_audio_attachment(
                attachment_id, session_attachments, attachments_dir,
            )
            if att is None:
                return Result(
                    f"Not transcribed: unknown attachment {attachment_id!r} "
                    "in this session.",
                    failed=True,
                )
            audio_path = att.get("path")
            if not audio_path or not Path(audio_path).is_file():
                return Result(
                    f"Not transcribed: attachment {attachment_id!r} has no "
                    "readable file.",
                    failed=True,
                )
            media_type = att.get("media_type") or "application/octet-stream"
            probed = att.get("metadata") or {}
            if not probed.get("duration_seconds"):
                probed = attachment_metadata.probe(Path(audio_path), media_type)
            provenance["attachment_id"] = attachment_id
            source_label = f"attachment {attachment_id} ({att['name']})"
        else:
            stage_dir = tempfile.mkdtemp(prefix="zero-stt-stage-")
            shown_path, audio_path, err = _stage_sandbox_audio_path(
                sandbox, path, stage_dir,
            )
            if err is not None:
                return Result(f"Not transcribed: {err}", failed=True)
            with open(audio_path, "rb") as stream:
                head = stream.read(4096)
            media_type = attachment_metadata.media_type(shown_path, head)
            probed = attachment_metadata.probe(Path(audio_path), media_type)
            provenance["path"] = shown_path
            source_label = shown_path

        duration = probed.get("duration_seconds")
        live_media = media if media is not None else Media()
        saved = active_stt_setting(limits, live_media, media_session)
        model_server = live_media.active_model_server(media_session)
        try:
            services = available_stt_services()
        except ModelServerError:
            return Result(
                "Not transcribed: speech-to-text configuration could not be read.",
                fields=provenance,
                failed=True,
            )

        chosen, reason = select_stt_service(services, model_server, saved, service)
        if chosen is None:
            return Result(
                f"Not transcribed: {reason}",
                fields={**provenance, "media_type": media_type},
                failed=True,
            )

        def check():
            if stop is not None and stop.is_set():
                raise Stopped()

        def invoke():
            kwargs = {
                "media_type": media_type,
                "language": language,
                "duration_seconds": duration,
                "seconds": DEFAULT_STT_TIMEOUT_SECONDS,
                "transport": live_media.transport,
                "check": check,
            }
            if chosen.api is LocalWhisper:
                kwargs["env"] = harness_free_env()
            return chosen.api.transcribe(chosen, audio_path, **kwargs)

        try:
            check()
            answer = _run_cancellable(invoke, stop, DEFAULT_STT_TIMEOUT_SECONDS)
        except (STTError, SandboxError) as exc:
            return Result(
                f"Transcription with {chosen.name} failed for "
                f"{source_label}. {exc}",
                fields={
                    **provenance,
                    "service": chosen.name,
                    "media_type": media_type,
                },
                failed=True,
            )
    finally:
        if stage_dir is not None:
            shutil.rmtree(stage_dir, ignore_errors=True)

    raw_text = answer.text
    if chosen.key:
        raw_text = raw_text.replace(chosen.key, "[redacted]")

    if notes is not None:
        notes["media"] = {
            "stt": True,
            "service": chosen.name,
            "billed": answer.billed,
        }

    fields = {
        **provenance,
        "service": chosen.name,
        "media_type": media_type,
    }
    if answer.model:
        fields["model"] = answer.model
    if answer.language:
        fields["language"] = answer.language
    final_duration = (
        answer.duration_seconds
        if answer.duration_seconds is not None
        else duration
    )
    if final_duration is not None:
        fields["duration_seconds"] = round(float(final_duration), 3)

    meta_bits = [f"service={chosen.name}"]
    if answer.model:
        meta_bits.append(f"model={answer.model}")
    if final_duration is not None:
        meta_bits.append(f"{final_duration:g}s")
    header = f"Transcript of {source_label} ({', '.join(meta_bits)}):"
    body = raw_text if raw_text else "(no speech detected)"
    return Result(f"{header}\n\n{body}", fields=fields)


def local_stt_binary(which=shutil.which):
    """Return `(binary_name, resolved_path)` for an installed local STT CLI."""
    return LocalWhisper.binary(which)


TOOLS = (transcribe_audio,)
