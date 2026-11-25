"""Attachment entry declarations and neutral text projection."""

from core.entries import Entry, EntrySpec

ATTACHMENT_TYPE = "attachments/file"
METADATA_KEYS = ("width", "height", "duration_seconds", "pages")
PUBLIC_FIELDS = (
    "attachment_id", "name", "media_type", "size_bytes", "sha256",
    "delivery", "path", *METADATA_KEYS,
)


def project_attachment(entry, *, reason=None):
    """Neutral text description of an attached file reference."""
    name = entry.get("name") or entry.get("attachment_id") or "file"
    media = entry.get("media_type") or "application/octet-stream"
    size = entry.get("size_bytes")
    parts = [media]
    if isinstance(size, int) and not isinstance(size, bool):
        parts.append(f"{size} bytes")
    width, height = entry.get("width"), entry.get("height")
    if (isinstance(width, int) and not isinstance(width, bool)
            and isinstance(height, int) and not isinstance(height, bool)):
        parts.append(f"{width}x{height}")
    duration = entry.get("duration_seconds")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        parts.append(f"{duration}s")
    pages = entry.get("pages")
    if isinstance(pages, int) and not isinstance(pages, bool):
        parts.append(f"{pages} pages")
    path = entry.get("path")
    detail = ", ".join(parts)
    note = f"; {reason}" if isinstance(reason, str) and reason.strip() else ""
    if path:
        return f"[Attached file: {name} ({detail}) at {path}{note}]"
    return f"[Attached file: {name} ({detail}){note}]"


ENTRY_SPECS = (
    EntrySpec(
        ATTACHMENT_TYPE,
        frozenset(("attachment", "model_visible", "searchable")),
        project=project_attachment,
        role="attachment",
        public_fields=PUBLIC_FIELDS,
        required_fields=("name", "media_type", "size_bytes"),
        fields_schema={
            "attachment_id": str,
            "name": str,
            "media_type": str,
            "size_bytes": int,
            "sha256": str,
            "delivery": str,
            "path": (str, type(None)),
            "width": (int, type(None)),
            "height": (int, type(None)),
            "duration_seconds": (int, float, type(None)),
            "pages": (int, type(None)),
        },
        redact=False,
    ),
)


def make_attachment_entry(
    attachment_id,
    *,
    name,
    media_type,
    size_bytes,
    sha256,
    path=None,
    delivery="inline",
    metadata=None,
):
    """Build an attachments/file child entry with origin=None to inherit parent."""
    fields = {
        "attachment_id": attachment_id,
        "name": name,
        "media_type": media_type,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "delivery": delivery,
    }
    if path is not None:
        fields["path"] = str(path)
    if isinstance(metadata, dict):
        for key in METADATA_KEYS:
            value = metadata.get(key)
            if value is not None:
                fields[key] = value
    return Entry(ATTACHMENT_TYPE, origin=None, fields=fields)


def attachment_nodes(entry):
    """Direct attachments/file child entries on a timeline entry."""
    if entry is None or not entry.entries:
        return ()
    return tuple(
        child for child in entry.entries if child.type == ATTACHMENT_TYPE
    )


def attachment_ids(entry):
    """Attachment IDs carried by a timeline entry's direct children."""
    return [
        aid for child in attachment_nodes(entry)
        if isinstance((aid := child.get("attachment_id")), str) and aid
    ]


def attachment_label(entry):
    """Non-blank display label built from attached filenames, or empty string."""
    names = [
        child.get("name") or child.get("attachment_id") or "attachment"
        for child in attachment_nodes(entry)
    ]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return f"{names[0]} (+{len(names) - 1} more)"
