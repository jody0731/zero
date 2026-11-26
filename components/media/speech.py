"""Deduplicated spoken replies (Listen + Auto-speak) with timeline cache.

Synthesis resolves the session's active/pinned `tts_service` under
Same-Provider Default (never silently billing another paid server), saves the
generated audio into the session's attachment store, and persists a non-
`model_visible` `media/speech_cache` timeline entry keyed by
`(reply_entry_id, text_sha256, server, model, voice)` through the session's
single writer.

An in-flight deduplication claim is held across both Listen and Auto-speak
until BOTH audio attachment publication AND `media/speech_cache` timeline
persistence complete, eliminating double synthesis across concurrent clicks,
active turns, replay, and fork.
"""

from concurrent.futures import Future
import hashlib
import re
import threading

from components.attachments.store import AttachmentError, LimitExceeded
from components.media import transcribe
from components.media.entries import find_cached_speech, make_speech_cache_entry
from components.media.tools import (
    DEFAULT_TTS_TIMEOUT_SECONDS,
    _persist_generated_bytes,
    available_media_services,
)
from components.model import SERVICE_CURRENT_SERVER, SERVICE_OFF, servers
from components.model.apis import select_utility_service
from components.model.apis.tts import TTSError
from components.model.servers import ModelServerError
from core import entries


class SpeechError(Exception):
    """A spoken-reply synthesis error safe to show to callers."""


_FENCE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def clean_speech_text(text):
    """Strip fenced code blocks and light Markdown punctuation for Listen."""
    if not isinstance(text, str):
        return ""
    cleaned = _FENCE_RE.sub(" ", text)
    cleaned = _LINK_RE.sub(r"\1", cleaned)
    cleaned = _INLINE_CODE_RE.sub(r"\1", cleaned)
    cleaned = re.sub(r"^\s*#{1,6}\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def extract_reply_text(records, reply_entry_id=None):
    """Return `(reply_entry_id, text)` for the requested or latest assistant reply."""
    for record in reversed(records):
        entry = record.entry
        if entry.type != "chat/assistant_reply":
            continue
        if reply_entry_id is not None and entry.id != reply_entry_id:
            continue
        text = entries.combined_text(entry, "text", origin="model")
        if not text:
            reply_fields = entry.get("reply")
            if isinstance(reply_fields, dict):
                text = (reply_fields.get("answer") or "").strip()
        if text and text.strip():
            return entry.id, text.strip()
        if reply_entry_id is not None:
            return entry.id, ""
    return None, ""


class SpeechManager:
    """Server-side Listen & Auto-speak coordinator with single-flight dedup."""

    def __init__(
        self,
        *,
        transport=None,
        ws_connect=None,
        services_provider=None,
    ):
        self.transport = transport
        self.ws_connect = ws_connect
        self._services_provider = (
            services_provider or (lambda: available_media_services("tts"))
        )
        self._lock = threading.Lock()
        self._inflight: dict[tuple, Future] = {}
        self._active_count = 0

    def busy_with(self):
        with self._lock:
            keys = sorted(f"{k[0]}:{k[1]}" for k in self._inflight)
        return {"speech": keys} if keys else {}

    def resolve_service(self, *, model, tts_service, requested=None):
        saved = SERVICE_CURRENT_SERVER if tts_service is None else tts_service
        if saved == SERVICE_OFF:
            raise SpeechError("Text-to-speech is off in settings.")
        model_server = None
        if isinstance(model, str) and model.strip():
            try:
                model_server, _ = servers.model_address(
                    model.strip(), servers.table(),
                )
            except ModelServerError:
                model_server = None
        try:
            available = self._services_provider()
        except ModelServerError as exc:
            raise SpeechError(str(exc)) from None
        chosen, reason = select_utility_service(
            available,
            model_server,
            saved,
            requested,
            label="Text-to-speech service",
        )
        if chosen is None:
            raise SpeechError(reason)
        return chosen

    def synthesize_text(
        self,
        text,
        *,
        model=None,
        tts_service=None,
        service=None,
        voice=None,
        instructions=None,
    ):
        """Synthesize arbitrary text directly (no session cache entry)."""
        clean = clean_speech_text(text)
        if not clean:
            raise SpeechError("No speakable text to synthesize.")
        chosen = self.resolve_service(
            model=model, tts_service=tts_service, requested=service,
        )
        try:
            answer = chosen.api.synthesize(
                chosen,
                clean,
                voice=voice,
                instructions=instructions,
                seconds=DEFAULT_TTS_TIMEOUT_SECONDS,
                transport=self.transport,
                ws_connect=self.ws_connect,
            )
        except TTSError as exc:
            raise SpeechError(str(exc)) from None
        return {
            "audio": answer.audio,
            "media_type": answer.media_type,
            "service": chosen.name,
            "model": answer.model,
            "voice": answer.voice,
            "size_bytes": len(answer.audio),
        }

    def synthesize_reply(
        self,
        *,
        sid,
        reply_entry_id,
        text,
        model,
        tts_service,
        session_attachments,
        read_records,
        prepare_cache_writer,
        append_cache_entry,
        service=None,
        voice=None,
        auto_speak=False,
        limits=None,
        allow_over_limit=False,
    ):
        """Synthesize a session reply with cache lookup and in-flight dedup.

        The per-key in-flight claim is held until BOTH audio publication into
        `session_attachments` AND `media/speech_cache` persistence through the
        session's single writer complete.
        """
        clean = clean_speech_text(text)
        if not clean:
            raise SpeechError("That reply has no speakable text.")
        chosen = self.resolve_service(
            model=model, tts_service=tts_service, requested=service,
        )
        chosen_voice = (
            voice.strip()
            if isinstance(voice, str) and voice.strip()
            else getattr(chosen.api, "DEFAULT_VOICE", "")
        )
        text_sha256 = hashlib.sha256(clean.encode("utf-8")).hexdigest()
        cache_key = (
            sid,
            reply_entry_id,
            text_sha256,
            chosen.name,
            chosen.model or "",
            chosen_voice or "",
        )

        # Fast path: check session branch log cache before acquiring in-flight claim.
        records = read_records()
        hit = find_cached_speech(
            records,
            reply_entry_id=reply_entry_id,
            text_sha256=text_sha256,
            server=chosen.name,
            model=chosen.model or "",
            voice=chosen_voice or "",
        )
        if hit is not None and session_attachments.resolve(hit["attachment_id"]):
            return {
                "attachment_id": hit["attachment_id"],
                "media_type": hit["media_type"],
                "size_bytes": hit["size_bytes"],
                "service": chosen.name,
                "model": chosen.model,
                "voice": chosen_voice or None,
                "reply_entry_id": reply_entry_id,
                "cached": True,
            }

        # Single-flight deduplication claim across Listen and Auto-speak.
        owner = False
        with self._lock:
            existing = self._inflight.get(cache_key)
            if existing is None:
                future = Future()
                self._inflight[cache_key] = future
                owner = True
            else:
                future = existing

        if not owner:
            result = future.result()
            return {**result, "cached": True}

        try:
            # Re-check cache after winning the claim in case a previous writer
            # just finished committing before we took the slot.
            records = read_records()
            hit = find_cached_speech(
                records,
                reply_entry_id=reply_entry_id,
                text_sha256=text_sha256,
                server=chosen.name,
                model=chosen.model or "",
                voice=chosen_voice or "",
            )
            if hit is not None and session_attachments.resolve(hit["attachment_id"]):
                result = {
                    "attachment_id": hit["attachment_id"],
                    "media_type": hit["media_type"],
                    "size_bytes": hit["size_bytes"],
                    "service": chosen.name,
                    "model": chosen.model,
                    "voice": chosen_voice or None,
                    "reply_entry_id": reply_entry_id,
                    "cached": True,
                }
                future.set_result(result)
                return result

            prepare_cache_writer()
            try:
                answer = chosen.api.synthesize(
                    chosen,
                    clean,
                    voice=chosen_voice or None,
                    seconds=DEFAULT_TTS_TIMEOUT_SECONDS,
                    transport=self.transport,
                    ws_connect=self.ws_connect,
                )
            except TTSError as exc:
                raise SpeechError(str(exc)) from None

            ext = ".wav" if "wav" in (answer.media_type or "") else ".mp3"
            filename = f"reply-{reply_entry_id[:8]}{ext}"
            try:
                meta, att_entry = _persist_generated_bytes(
                    answer.audio,
                    filename=filename,
                    media_type=answer.media_type,
                    session_attachments=session_attachments,
                    limits=limits,
                    allow_over_limit=bool(allow_over_limit),
                )
            except (LimitExceeded, AttachmentError) as exc:
                raise SpeechError(
                    f"Could not store spoken reply audio: {exc}."
                ) from None
            cache_entry = make_speech_cache_entry(
                reply_entry_id=reply_entry_id,
                text_sha256=text_sha256,
                server=chosen.name,
                model=chosen.model or "",
                voice=answer.voice or chosen_voice or "",
                attachment_id=meta["attachment_id"],
                media_type=meta["media_type"],
                size_bytes=meta["size_bytes"],
                attachment_entry=att_entry,
                auto_speak=auto_speak,
            )
            append_cache_entry(cache_entry)
            result = {
                "attachment_id": meta["attachment_id"],
                "media_type": meta["media_type"],
                "size_bytes": meta["size_bytes"],
                "service": chosen.name,
                "model": answer.model or chosen.model,
                "voice": answer.voice or chosen_voice or None,
                "reply_entry_id": reply_entry_id,
                "cached": False,
            }
            future.set_result(result)
            return result
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        finally:
            with self._lock:
                if self._inflight.get(cache_key) is future:
                    self._inflight.pop(cache_key, None)
