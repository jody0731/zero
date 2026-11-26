"""Permission-gated media generation tools: images, speech, and video.

All three tools obey Same-Provider Default (live session setting + live active
model server; never silently billing another paid server), ask in default mode
and go through the judge in auto/auto+, persist generated outputs directly into
the session's attachment store (honoring soft caps), and return inline
deliverable cards (`attachments/file` with `delivery="reference"`).

Video generation additionally claims its durable op record BEFORE submitting
the HTTP POST (via callbacks injected into `session.tool_context` by the
media service), disables transport retries on submit, and distinguishes local
stop-wait cancellation from upstream billing.
"""

from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import tempfile
import threading
import time

from components.attachments import metadata as attachment_metadata
from components.attachments.entries import make_attachment_entry
from components.attachments.store import AttachmentError, LimitExceeded, short_id_of
from components.media import transcribe
from components.model import SERVICE_CURRENT_SERVER, SERVICE_OFF, servers
from components.model.apis import discover_utility_services, select_utility_service
from components.model.apis.images import ImageError
from components.model.apis.multimodal import IMAGE_MIME_TYPES, normalize_media_type
from components.model.apis.tts import TTSError
from components.model.apis.videos import VideoError, normalize_video_duration
from components.model.servers import ModelServerError
from core.asking import PARK
from core.control import Stopped
from core.llm import safe_error_text
from core.tools import Result, tool

DEFAULT_IMAGE_TIMEOUT_SECONDS = 180
DEFAULT_TTS_TIMEOUT_SECONDS = 60
DEFAULT_VIDEO_TIMEOUT_SECONDS = 600
PRE_EXECUTION_REJECT_STATUSES = frozenset({400, 401, 403, 404, 422})


def available_media_services(capability):
    """Discover eligible services for `capability` (`tts`, `image`, `video`)."""
    return servers.utility_services(capability, discover_utility_services)


def active_media_setting(setting_name, limits, media=None, session=None):
    """Authoritative utility setting: live session.settings first, else limits."""
    live = session or (media._session if media is not None else None)
    if live is not None:
        session_settings = getattr(live, "settings", None)
        if isinstance(session_settings, dict) and setting_name in session_settings:
            val = session_settings[setting_name]
            return SERVICE_CURRENT_SERVER if val is None else val
    if limits is not None and setting_name in limits:
        val = limits[setting_name]
        return SERVICE_CURRENT_SERVER if val is None else val
    return SERVICE_CURRENT_SERVER


def _describe_service_arg(capability, setting_name, label):
    def describe(limits, context):
        text = (
            f"Optional {label} service name for this call. "
            "Omit to use the current session choice."
        )
        media = context.get("media")
        if media is None:
            return text
        session = context.get("media_session")
        saved = active_media_setting(setting_name, limits, media, session)
        if saved == SERVICE_OFF:
            return f"{label.capitalize()} is off in this session's settings."
        try:
            services = available_media_services(capability)
        except ModelServerError:
            return text
        model_server = media.active_model_server(session)
        chosen, _ = select_utility_service(
            services, model_server, saved, label=label.capitalize(),
        )
        if chosen is not None:
            text += f" The current choice is {chosen.name}."
        return text

    return describe


def _media_crossing(rule_name, action_phrase):
    def crossing(args):
        return {
            "rule": rule_name,
            "fact": (
                f"{action_phrase} via an external media service "
                "(may incur provider charges)."
            ),
        }

    return crossing


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
                raise TimeoutError(
                    f"Media request did not finish within {seconds:g} s."
                ) from None
            continue
        if ok:
            return val
        raise val


def _display_and_disk_name(filename, fallback="output.bin"):
    """Keep stand-ins intact in display names while sanitizing the on-disk basename."""
    raw = filename.strip() if isinstance(filename, str) else ""
    if not raw:
        return fallback, fallback
    # Strip leading directory components only outside stand-in brackets.
    parts = re.split(r"(⟦[^⟧]+⟧)", raw)
    last_slash_part = -1
    for idx in range(0, len(parts), 2):
        if "/" in parts[idx] or "\\" in parts[idx]:
            last_slash_part = idx
    if last_slash_part >= 0:
        tail = re.split(r"[/\\]", parts[last_slash_part])[-1]
        display = tail + "".join(parts[last_slash_part + 1:])
    else:
        display = raw
    display = display.strip() or fallback
    disk = re.sub(r"[/\\\x00]+", "_", display).strip(".") or fallback
    return display, disk


def _persist_generated_bytes(
    raw_bytes,
    *,
    filename,
    media_type,
    session_attachments=None,
    limits=None,
    allow_over_limit=False,
    ask_over_limit=None,
    fallback_dir=None,
):
    """Store generated media bytes into the session attachment store (or temp file)."""
    tmp_dir = tempfile.mkdtemp(prefix="zero-media-out-")
    try:
        display_name, disk_name = _display_and_disk_name(filename)
        out_path = Path(tmp_dir) / disk_name
        out_path.write_bytes(raw_bytes)
        if session_attachments is not None:
            try:
                return session_attachments.add_file(
                    out_path,
                    name=display_name,
                    media_hint=media_type,
                    delivery="reference",
                    allow_over_limit=allow_over_limit,
                    limits=limits,
                )
            except LimitExceeded as exc:
                if (
                    callable(ask_over_limit)
                    and ask_over_limit is not PARK
                    and bool(ask_over_limit(
                        f"store {display_name}",
                        f"store generated media ({len(raw_bytes):,} bytes): {exc}",
                    ))
                ):
                    return session_attachments.add_file(
                        out_path,
                        name=display_name,
                        media_hint=media_type,
                        delivery="reference",
                        allow_over_limit=True,
                        limits=limits,
                    )
                raise
        # Standalone session without a server attachment store.
        target_dir = Path(fallback_dir) if fallback_dir else Path(
            tempfile.mkdtemp(prefix="zero-media-standalone-")
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        final_path = target_dir / disk_name
        shutil.copy2(out_path, final_path)
        sha = hashlib.sha256(raw_bytes).hexdigest()
        probed = attachment_metadata.probe_bytes(raw_bytes, media_type)
        meta = {
            "attachment_id": "",
            "name": display_name,
            "media_type": media_type,
            "size_bytes": len(raw_bytes),
            "sha256": sha,
            "path": str(final_path),
            "metadata": probed,
        }
        entry = make_attachment_entry(
            "",
            name=meta["name"],
            media_type=media_type,
            size_bytes=len(raw_bytes),
            sha256=sha,
            path=str(final_path),
            delivery="reference",
            metadata=probed,
        )
        return meta, entry
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _source_image_on_disk(attachment_id, folder):
    prefix = f"{short_id_of(attachment_id)}-"
    paths = sorted(Path(folder).glob(f"{prefix}*"))
    if not paths or not paths[0].is_file():
        return None
    path = paths[0]
    if attachment_metadata.is_svg(path.name):
        mime = attachment_metadata.SVG_MEDIA_TYPE
    else:
        with path.open("rb") as stream:
            head = stream.read(attachment_metadata.SVG_HEADER_BYTES)
            mime = attachment_metadata.media_type(path.name, head)
    return {"path": path, "name": path.name.removeprefix(prefix), "media_type": mime}


def _load_source_images(
    attachment_ids, session_attachments=None, attachments_dir=None,
):
    """Resolve optional source image attachment IDs into `(name, bytes, mime)`."""
    if not attachment_ids:
        return [], None
    loaded = []
    for aid in attachment_ids:
        att = None
        if session_attachments is not None:
            att = session_attachments.resolve(aid)
        if att is None and attachments_dir is not None:
            att = _source_image_on_disk(aid, attachments_dir)
        if att is None:
            return None, f"unknown source attachment {aid!r} in this session"
        fname = att.get("name") or "image.png"
        mime = (
            attachment_metadata.SVG_MEDIA_TYPE if attachment_metadata.is_svg(fname)
            else normalize_media_type(att.get("media_type"))
        )
        if mime not in IMAGE_MIME_TYPES:
            return None, f"source attachment {aid!r} has unsupported image type {mime}"
        src_path = att.get("path")
        if not src_path or not Path(src_path).is_file():
            return None, f"source attachment {aid!r} has no readable file"
        with Path(src_path).open("rb") as stream:
            head = stream.read(attachment_metadata.SVG_HEADER_BYTES)
            if attachment_metadata.is_svg(fname, head):
                return None, f"source attachment {aid!r} has unsupported SVG image type"
            loaded.append((fname, head + stream.read(), mime))
    return loaded, None


def _image_summary(args):
    prompt = str(args.get("prompt") or "").strip().replace("\n", " ")
    if len(prompt) > 60:
        prompt = prompt[:59] + "…"
    return f"generate_image {prompt!r}"


def _speech_summary(args):
    text = str(args.get("text") or "").strip().replace("\n", " ")
    if len(text) > 60:
        text = text[:59] + "…"
    return f"generate_speech {text!r}"


def _video_summary(args):
    prompt = str(args.get("prompt") or "").strip().replace("\n", " ")
    if len(prompt) > 60:
        prompt = prompt[:59] + "…"
    return f"generate_video {prompt!r}"


def _capability_ready(capability, setting_key):
    """Offer media-generation tool only when active/pinned server supports it."""
    def check(context):
        media = context.get("media")
        if media is None:
            return False
        session = context.get("media_session")
        limits = context.get("limits")
        saved = active_media_setting(setting_key, limits, media, session)
        if saved == SERVICE_OFF:
            return False
        model_server = media.active_model_server(session)
        if saved == SERVICE_CURRENT_SERVER and model_server is None:
            return False
        try:
            services = available_media_services(capability)
        except ModelServerError:
            return False
        chosen, _ = select_utility_service(services, model_server, saved)
        return chosen is not None

    return check


@tool(
    "Generate or edit images using the session's configured image service "
    "(Same-Provider Default: active model server only unless explicitly set). "
    "Saves outputs into the session attachment store as deliverable cards.",
    optional=(
        "size", "quality", "aspect_ratio", "count", "source_attachment_ids",
        "service", "allow_over_limit",
    ),
    ready=_capability_ready("image", "image_service"),
    summary=_image_summary,
    metadata={
        "permissions": {
            "crossing": _media_crossing("generate_image", "Generate an image"),
        },
    },
    prompt=("string", "Detailed image generation or edit prompt"),
    size=("string", "Optional image dimensions (e.g. 1024x1024)"),
    quality=("string", "Optional quality setting (e.g. low, medium, high)"),
    aspect_ratio=("string", "Optional aspect ratio (e.g. 16:9, 1:1)"),
    count=("integer", "Number of images to generate (1 to 4)"),
    source_attachment_ids=(
        "array",
        "Optional image attachment IDs to edit or reference",
    ),
    service=(
        "string",
        _describe_service_arg("image", "image_service", "image generation"),
    ),
    allow_over_limit=(
        "boolean",
        "Allow storing output exceeding soft attachment size limits",
    ),
    fields={
        "service": ("string", "Image service used"),
        "model": ("string", "Image model used"),
        "count": ("integer", "Number of generated images"),
        "attachment_ids": ("array", "Stored attachment IDs"),
        "revised_prompt": ("string", "Provider revised prompt if returned"),
    },
)
def generate_image(
    prompt,
    size=None,
    quality=None,
    aspect_ratio=None,
    count=1,
    source_attachment_ids=None,
    service=None,
    allow_over_limit=False,
    limits=None,
    media=None,
    media_session=None,
    session_attachments=None,
    attachments_dir=None,
    media_ask=None,
    stop=None,
    notes=None,
):
    """Generate or edit images and store them as session deliverable attachments."""
    if not isinstance(prompt, str) or not prompt.strip():
        return Result("Not generated: prompt must not be empty.", failed=True)
    source_images, err = _load_source_images(
        source_attachment_ids, session_attachments, attachments_dir,
    )
    if err is not None:
        return Result(f"Not generated: {err}.", failed=True)

    live_media = media if media is not None else transcribe.Media()
    saved = active_media_setting("image_service", limits, live_media, media_session)
    model_server = live_media.active_model_server(media_session)
    try:
        services = available_media_services("image")
    except ModelServerError:
        return Result(
            "Not generated: image service configuration could not be read.",
            failed=True,
        )
    chosen, reason = select_utility_service(
        services, model_server, saved, service, label="Image generation service",
    )
    if chosen is None:
        return Result(f"Not generated: {reason}", failed=True)

    def check():
        if stop is not None and stop.is_set():
            raise Stopped()

    def invoke():
        return chosen.api.generate(
            chosen,
            prompt,
            size=size,
            quality=quality,
            count=count,
            aspect_ratio=aspect_ratio,
            source_images=source_images,
            seconds=DEFAULT_IMAGE_TIMEOUT_SECONDS,
            transport=live_media.transport,
            check=check,
        )

    try:
        check()
        answer = _run_cancellable(invoke, stop, DEFAULT_IMAGE_TIMEOUT_SECONDS)
    except (ImageError, TimeoutError) as exc:
        return Result(
            f"Image generation with {chosen.name} failed. {exc}",
            fields={"service": chosen.name},
            failed=True,
        )

    entries_out = []
    stored_ids = []
    summaries = []
    for item in answer.images:
        try:
            meta, entry = _persist_generated_bytes(
                item.data,
                filename=item.filename,
                media_type=item.media_type,
                session_attachments=session_attachments,
                limits=limits,
                allow_over_limit=bool(allow_over_limit),
                ask_over_limit=media_ask,
            )
        except (LimitExceeded, AttachmentError) as exc:
            return Result(
                f"Generated image could not be stored in session attachments: {exc}.",
                fields={"service": chosen.name},
                failed=True,
            )
        entries_out.append(entry)
        if meta["attachment_id"]:
            stored_ids.append(meta["attachment_id"])
        summaries.append(
            f"{meta['name']} ({meta['media_type']}, {meta['size_bytes']:,} bytes)"
        )

    if notes is not None:
        notes["media"] = {
            "image": True, "service": chosen.name, "billed": answer.billed,
        }

    fields = {
        "service": chosen.name,
        "count": len(entries_out),
        "attachment_ids": stored_ids,
    }
    if answer.model:
        fields["model"] = answer.model
    if answer.revised_prompt:
        fields["revised_prompt"] = answer.revised_prompt
    text = f"Generated {len(entries_out)} image(s) via {chosen.name}: " + ", ".join(
        summaries
    )
    return Result(text, fields=fields, entries=tuple(entries_out))


@tool(
    "Synthesize spoken audio from text using the session's configured "
    "text-to-speech service (Same-Provider Default). Saves the audio file "
    "into the session attachment store as a deliverable card.",
    optional=("voice", "instructions", "filename", "service", "allow_over_limit"),
    ready=_capability_ready("tts", "tts_service"),
    summary=_speech_summary,
    metadata={
        "permissions": {
            "crossing": _media_crossing("generate_speech", "Synthesize speech"),
        },
        "tool_boundary": {"keep_stand_ins": ("filename",)},
    },
    text=("string", "Text to synthesize into spoken audio"),
    voice=("string", "Optional provider voice name"),
    instructions=("string", "Optional delivery or style instructions"),
    filename=("string", "Optional output filename"),
    service=(
        "string",
        _describe_service_arg("tts", "tts_service", "text-to-speech"),
    ),
    allow_over_limit=(
        "boolean",
        "Allow storing output exceeding soft attachment size limits",
    ),
    fields={
        "service": ("string", "TTS service used"),
        "model": ("string", "TTS model used"),
        "voice": ("string", "Voice used"),
        "attachment_id": ("string", "Stored attachment ID"),
        "media_type": ("string", "Audio MIME type"),
        "size_bytes": ("integer", "Audio size in bytes"),
    },
)
def generate_speech(
    text,
    voice=None,
    instructions=None,
    filename=None,
    service=None,
    allow_over_limit=False,
    limits=None,
    media=None,
    media_session=None,
    session_attachments=None,
    media_ask=None,
    stop=None,
    notes=None,
):
    """Synthesize speech audio and store it as a session deliverable attachment."""
    if not isinstance(text, str) or not text.strip():
        return Result("Not synthesized: text must not be empty.", failed=True)

    live_media = media if media is not None else transcribe.Media()
    saved = active_media_setting("tts_service", limits, live_media, media_session)
    model_server = live_media.active_model_server(media_session)
    try:
        services = available_media_services("tts")
    except ModelServerError:
        return Result(
            "Not synthesized: speech service configuration could not be read.",
            failed=True,
        )
    chosen, reason = select_utility_service(
        services, model_server, saved, service, label="Text-to-speech service",
    )
    if chosen is None:
        return Result(f"Not synthesized: {reason}", failed=True)

    def check():
        if stop is not None and stop.is_set():
            raise Stopped()

    def invoke():
        return chosen.api.synthesize(
            chosen,
            text,
            voice=voice,
            instructions=instructions,
            seconds=DEFAULT_TTS_TIMEOUT_SECONDS,
            transport=live_media.transport,
            ws_connect=getattr(live_media, "ws_connect", None),
            check=check,
        )

    try:
        check()
        answer = _run_cancellable(invoke, stop, DEFAULT_TTS_TIMEOUT_SECONDS)
    except (TTSError, TimeoutError) as exc:
        detail = (
            safe_error_text(str(exc), key=chosen.key) if isinstance(exc, TTSError)
            else "The speech service did not respond in time."
        )
        return Result(
            f"Speech synthesis with {chosen.name} failed. {detail}",
            fields={"service": chosen.name},
            failed=True,
        )

    ext = ".wav" if "wav" in (answer.media_type or "") else ".mp3"
    out_name = (
        filename.strip()
        if isinstance(filename, str) and filename.strip()
        else f"speech{ext}"
    )
    try:
        meta, entry = _persist_generated_bytes(
            answer.audio,
            filename=out_name,
            media_type=answer.media_type,
            session_attachments=session_attachments,
            limits=limits,
            allow_over_limit=bool(allow_over_limit),
            ask_over_limit=media_ask,
        )
    except (LimitExceeded, AttachmentError) as exc:
        return Result(
            f"Generated speech could not be stored in session attachments: {exc}.",
            fields={"service": chosen.name},
            failed=True,
        )

    if notes is not None:
        notes["media"] = {
            "tts": True, "service": chosen.name, "billed": answer.billed,
        }

    fields = {
        "service": chosen.name,
        "attachment_id": meta["attachment_id"],
        "media_type": meta["media_type"],
        "size_bytes": meta["size_bytes"],
    }
    if answer.model:
        fields["model"] = answer.model
    if answer.voice:
        fields["voice"] = answer.voice
    return Result(
        f"Synthesized {meta['name']} ({meta['media_type']}, "
        f"{meta['size_bytes']:,} bytes) via {chosen.name}.",
        fields=fields,
        entries=(entry,),
    )


def _video_request_fingerprint(
    service_name,
    model_name,
    prompt,
    resolution,
    duration,
    aspect_ratio,
    reference_images=(),
    *,
    legacy=False,
):
    """Hash the request's fields and ordered reference images."""
    parts = [
        service_name or "",
        model_name or "",
        prompt.strip(),
        str(resolution or ""),
        str(duration if duration is not None else ""),
        str(aspect_ratio or ""),
    ]
    images = [
        (mime or "image/png", hashlib.sha256(raw).hexdigest())
        for _, raw, mime in (reference_images or ())
    ]
    if legacy:
        parts.append(",".join(digest for _, digest in images))
        serialized = "\x1f".join(parts)
    else:
        serialized = json.dumps(
            [*parts, images], separators=(",", ":"), ensure_ascii=False,
        )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _video_op_key(
    session_id,
    service_name,
    model_name,
    prompt,
    resolution,
    duration,
    aspect_ratio,
    idempotency_key=None,
    reference_images=(),
    *,
    legacy=False,
):
    if isinstance(idempotency_key, str) and idempotency_key.strip():
        parts = [session_id or "standalone", "explicit", idempotency_key.strip()]
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    fingerprint = _video_request_fingerprint(
        service_name,
        model_name,
        prompt,
        resolution,
        duration,
        aspect_ratio,
        reference_images,
        legacy=legacy,
    )
    parts = [session_id or "standalone", "auto", fingerprint]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


@tool(
    "Generate a video clip using the session's configured video service "
    "(Same-Provider Default). Claims the operation durably before submit so "
    "restarts never double-bill, and saves the clip into session attachments.",
    optional=(
        "resolution", "duration", "aspect_ratio", "reference_attachment_ids",
        "service", "idempotency_key", "allow_over_limit",
    ),
    ready=_capability_ready("video", "video_service"),
    summary=_video_summary,
    metadata={
        "permissions": {
            "crossing": _media_crossing("generate_video", "Generate a video"),
        },
    },
    prompt=("string", "Detailed video generation prompt"),
    resolution=("string", "Optional target resolution (e.g. 720p, 1080p)"),
    duration=("integer", "Optional target clip duration in seconds"),
    aspect_ratio=("string", "Optional aspect ratio (e.g. 16:9, 9:16)"),
    reference_attachment_ids=(
        "array",
        "Optional reference image attachment IDs",
    ),
    service=(
        "string",
        _describe_service_arg("video", "video_service", "video generation"),
    ),
    idempotency_key=(
        "string",
        "Optional caller key identifying one distinct generation request",
    ),
    allow_over_limit=(
        "boolean",
        "Allow storing output exceeding soft attachment size limits",
    ),
    fields={
        "service": ("string", "Video service used"),
        "model": ("string", "Video model used"),
        "attachment_id": ("string", "Stored attachment ID"),
        "media_type": ("string", "Video MIME type"),
        "size_bytes": ("integer", "Video size in bytes"),
        "interaction_id": ("string", "Upstream video operation ID"),
        "uri": ("string", "Upstream video URI handle if present"),
        "outcome": ("string", "Durable op outcome"),
    },
)
def generate_video(
    prompt,
    resolution=None,
    duration=None,
    aspect_ratio=None,
    reference_attachment_ids=None,
    service=None,
    idempotency_key=None,
    allow_over_limit=False,
    limits=None,
    media=None,
    media_session=None,
    session_attachments=None,
    attachments_dir=None,
    media_ask=None,
    video_op_claim=None,
    video_op_update=None,
    video_op_lock=None,
    stop=None,
    notes=None,
):
    """Generate a video clip with durable pre-submit claiming and zero retries."""
    if not isinstance(prompt, str) or not prompt.strip():
        return Result("Not generated: prompt must not be empty.", failed=True)
    try:
        encoded_duration = normalize_video_duration(duration)
    except VideoError as exc:
        return Result(f"Not generated: {exc}", failed=True)
    ref_images, err = _load_source_images(
        reference_attachment_ids, session_attachments, attachments_dir,
    )
    if err is not None:
        return Result(f"Not generated: {err}.", failed=True)

    live_media = media if media is not None else transcribe.Media()
    saved = active_media_setting("video_service", limits, live_media, media_session)
    model_server = live_media.active_model_server(media_session)
    try:
        services = available_media_services("video")
    except ModelServerError:
        return Result(
            "Not generated: video service configuration could not be read.",
            failed=True,
        )
    chosen, reason = select_utility_service(
        services, model_server, saved, service, label="Video generation service",
    )
    if chosen is None:
        return Result(f"Not generated: {reason}", failed=True)

    if stop is not None and stop.is_set():
        raise Stopped()

    if not callable(video_op_claim):
        return Result(
            "Not generated: durable video operation tracking is unavailable; "
            "refusing to send a paid video request without double-billing "
            "protection.",
            fields={"service": chosen.name, "outcome": "claim_unavailable"},
            failed=True,
        )

    sid = getattr(session_attachments, "sid", None)
    request_fingerprint = _video_request_fingerprint(
        chosen.name,
        chosen.model,
        prompt,
        resolution,
        encoded_duration,
        aspect_ratio,
        ref_images,
    )
    explicit_key = (
        idempotency_key.strip()
        if isinstance(idempotency_key, str) and idempotency_key.strip()
        else None
    )
    op_key = _video_op_key(
        sid,
        chosen.name,
        chosen.model,
        prompt,
        resolution,
        encoded_duration,
        aspect_ratio,
        idempotency_key=explicit_key,
        reference_images=ref_images,
    )

    claim_fields = {
        "service": chosen.name,
        "model": chosen.model or "",
        "prompt_sha256": request_fingerprint,
    }
    if explicit_key is None:
        # The old automatic key is checked only for an existing claim.
        claim_fields["legacy_key"] = _video_op_key(
            sid, chosen.name, chosen.model, prompt, resolution, encoded_duration,
            aspect_ratio, reference_images=ref_images, legacy=True,
        )
    guard = (
        video_op_lock(op_key, stop=stop) if callable(video_op_lock) else nullcontext()
    )
    with guard:
        return _run_video_op(
            op_key, chosen, claim_fields, explicit_key, video_op_claim, video_op_update,
            prompt=prompt, resolution=resolution, duration=encoded_duration,
            aspect_ratio=aspect_ratio, reference_images=ref_images,
            live_media=live_media, session_attachments=session_attachments,
            limits=limits, allow_over_limit=allow_over_limit, media_ask=media_ask,
            stop=stop, notes=notes,
        )


def _completed_video(chosen, session_attachments, attachment_id, interaction_id=None):
    attachment = (
        session_attachments.resolve(attachment_id)
        if attachment_id and session_attachments is not None else None
    )
    if attachment is None:
        return Result(
            "Prior video operation already completed and may have been "
            "charged, but its stored attachment could not be resolved; "
            "refusing automatic resubmit to avoid double billing.",
            fields={
                "service": chosen.name,
                "interaction_id": interaction_id or "",
                "outcome": "completed_attachment_missing",
            },
            failed=True,
        )
    entry = make_attachment_entry(
        attachment_id,
        name=attachment["name"],
        media_type=attachment["media_type"],
        size_bytes=attachment["size_bytes"],
        sha256=attachment["sha256"],
        path=attachment["path"],
        delivery="reference",
        metadata=attachment.get("metadata"),
    )
    return Result(
        f"Returned prior generated video {attachment['name']} "
        f"(attachment {attachment_id}; no duplicate request sent).",
        fields={
            "service": chosen.name,
            "model": chosen.model or "",
            "attachment_id": attachment_id,
            "media_type": attachment["media_type"],
            "size_bytes": attachment["size_bytes"],
            "outcome": "reused_completed",
        },
        entries=(entry,),
    )


def _run_video_op(
    op_key, chosen, claim_fields, explicit_key, claim, update, *,
    prompt, resolution, duration, aspect_ratio, reference_images, live_media,
    session_attachments, limits, allow_over_limit, media_ask, stop, notes,
):
    # Claim the operation before submission so restarts never re-bill.
    prior = claim(op_key, **claim_fields)
    if prior is not None:
        if not isinstance(prior, dict):
            return Result(
                "Video generation operation already exists in an unrecognized "
                "state; outcome unknown, possibly charged. Refusing automatic "
                "resubmit to avoid double billing.",
                fields={
                    "service": chosen.name,
                    "outcome": "unknown_possibly_charged",
                },
                failed=True,
            )
        if any(
            prior.get(name) is not None and prior[name] != claim_fields[name]
            for name in ("service", "model", "prompt_sha256")
        ):
            label = (
                f"Video idempotency_key {explicit_key!r}"
                if explicit_key is not None else "This video operation"
            )
            return Result(
                f"{label} was already used for a different request; refusing "
                "to reuse it or submit a duplicate paid request.",
                fields={"service": chosen.name, "outcome": "key_mismatch"},
                failed=True,
            )
        if prior.get("status") == "completed":
            return _completed_video(
                chosen, session_attachments, prior.get("attachment_id"),
                prior.get("interaction_id"),
            )

    interaction_id = prior.get("interaction_id") if prior else None
    uri = prior.get("uri") if prior else None
    resuming = prior is not None and prior.get("status") == "submitted"
    if prior is not None and not (resuming and (interaction_id or uri)):
        return Result(
            "Video generation operation was already claimed before submit; "
            "outcome unknown, possibly charged. Refusing automatic resubmit "
            "to avoid double billing.",
            fields={"service": chosen.name, "outcome": "unknown_possibly_charged"},
            failed=True,
        )

    submitted_upstream = resuming

    def on_submitted(*, interaction_id=None, uri=None):
        nonlocal submitted_upstream
        submitted_upstream = True
        if callable(update):
            update(op_key, status="submitted", interaction_id=interaction_id, uri=uri)

    call_options = {
        "poll_interval": getattr(live_media, "video_poll_interval", 2.0),
        "seconds": DEFAULT_VIDEO_TIMEOUT_SECONDS,
        "transport": live_media.transport,
        "stop": stop,
        "on_submitted": on_submitted,
    }
    try:
        if resuming:
            answer = chosen.api.resume(
                chosen, interaction_id=interaction_id, uri=uri, **call_options,
            )
        else:
            answer = chosen.api.generate(
                chosen, prompt, resolution=resolution, duration=duration,
                aspect_ratio=aspect_ratio, reference_images=reference_images,
                **call_options,
            )
    except Stopped as exc:
        if submitted_upstream:
            detail = (
                "Stopped waiting locally for video generation; the upstream "
                "request was already submitted and may still incur charges."
            )
        else:
            detail = (
                "Stopped waiting locally during video submission; upstream "
                "outcome is unknown and may incur charges."
            )
        exc.output_detail = f"\n{detail}"
        if notes is not None:
            notes["video_stop_note"] = detail
        raise
    except VideoError as exc:
        if resuming:
            return Result(
                f"Prior video operation could not be retrieved without resubmitting: "
                f"{exc}",
                fields={
                    "service": chosen.name,
                    "interaction_id": interaction_id or "",
                    "outcome": "prior_handle_failed",
                },
                failed=True,
            )
        if (
            not submitted_upstream
            and exc.status_code in PRE_EXECUTION_REJECT_STATUSES
            and callable(update)
        ):
            update(op_key, status="failed")
        return Result(
            f"Video generation with {chosen.name} failed. {exc}",
            fields={"service": chosen.name, "outcome": "failed"},
            failed=True,
        )

    try:
        meta, entry = _persist_generated_bytes(
            answer.data,
            filename=answer.filename,
            media_type=answer.media_type,
            session_attachments=session_attachments,
            limits=limits,
            allow_over_limit=bool(allow_over_limit),
            ask_over_limit=media_ask,
        )
    except (LimitExceeded, AttachmentError) as exc:
        return Result(
            f"Generated video could not be stored in session attachments: {exc}.",
            fields={
                "service": chosen.name, "interaction_id": answer.interaction_id or "",
            },
            failed=True,
        )

    if callable(update):
        attachment_id = update(
            op_key,
            status="completed",
            interaction_id=answer.interaction_id,
            uri=answer.uri,
            attachment_id=meta["attachment_id"],
        )
        if attachment_id and attachment_id != meta["attachment_id"]:
            return _completed_video(chosen, session_attachments, attachment_id)

    if notes is not None:
        notes["media"] = {
            "video": True, "service": chosen.name, "billed": answer.billed,
        }

    fields = {
        "service": chosen.name,
        "attachment_id": meta["attachment_id"],
        "media_type": meta["media_type"],
        "size_bytes": meta["size_bytes"],
        "outcome": "completed",
    }
    if answer.model:
        fields["model"] = answer.model
    if answer.interaction_id:
        fields["interaction_id"] = answer.interaction_id
    if answer.uri:
        fields["uri"] = answer.uri
    return Result(
        f"Generated {meta['name']} ({meta['media_type']}, "
        f"{meta['size_bytes']:,} bytes) via {chosen.name}.",
        fields=fields,
        entries=(entry,),
    )


TOOLS = (generate_image, generate_speech, generate_video)
