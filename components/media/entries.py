"""Media component timeline entry declarations (non-model-visible caches)."""

from datetime import datetime
from uuid import uuid4

from core.entries import Entry, EntrySpec

SPEECH_CACHE_TYPE = "media/speech_cache"
SPEECH_CACHE_PUBLIC_FIELDS = (
    "reply_entry_id",
    "text_sha256",
    "server",
    "model",
    "voice",
    "attachment_id",
    "media_type",
    "size_bytes",
    "auto_speak",
)

ENTRY_SPECS = (
    EntrySpec(
        SPEECH_CACHE_TYPE,
        frozenset(("media",)),
        role="media",
        public_fields=SPEECH_CACHE_PUBLIC_FIELDS,
        redact=False,
    ),
)


def make_speech_cache_entry(
    *,
    reply_entry_id,
    text_sha256,
    server,
    model,
    voice,
    attachment_id,
    media_type,
    size_bytes,
    attachment_entry=None,
    auto_speak=False,
):
    """Build a non-model-visible `media/speech_cache` timeline entry."""
    fields = {
        "reply_entry_id": reply_entry_id,
        "text_sha256": text_sha256,
        "server": server,
        "model": model or "",
        "voice": voice or "",
        "attachment_id": attachment_id,
        "media_type": media_type,
        "size_bytes": int(size_bytes),
        "auto_speak": bool(auto_speak),
    }
    children = (attachment_entry,) if attachment_entry is not None else ()
    return Entry(
        SPEECH_CACHE_TYPE,
        id=uuid4().hex,
        origin="system",
        created_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        fields=fields,
        entries=children,
    )


def find_cached_speech(records, *, reply_entry_id, text_sha256, server, model, voice):
    """Return the newest matching `media/speech_cache` fields dict, or None."""
    want_model = model or ""
    want_voice = voice or ""
    for record in reversed(records):
        entry = record.entry
        if entry.type != SPEECH_CACHE_TYPE:
            continue
        if (
            entry.get("reply_entry_id") == reply_entry_id
            and entry.get("text_sha256") == text_sha256
            and entry.get("server") == server
            and (entry.get("model") or "") == want_model
            and (entry.get("voice") or "") == want_voice
            and isinstance(entry.get("attachment_id"), str)
            and entry.get("attachment_id")
        ):
            return {
                "attachment_id": entry.get("attachment_id"),
                "media_type": entry.get("media_type") or "audio/wav",
                "size_bytes": int(entry.get("size_bytes") or 0),
                "server": server,
                "model": entry.get("model") or None,
                "voice": entry.get("voice") or None,
                "reply_entry_id": reply_entry_id,
                "text_sha256": text_sha256,
            }
    return None
