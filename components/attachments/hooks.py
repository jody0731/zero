"""Session hooks for attachments: materialize hosted model media once into storage."""

import base64
import binascii
from collections.abc import Mapping
from dataclasses import replace
import hashlib
from pathlib import Path
import re
import shutil
import tempfile

from components.attachments import metadata as attachment_metadata
from components.attachments.entries import ATTACHMENT_TYPE, make_attachment_entry
from components.attachments.store import AttachmentError, LimitExceeded
from core import entries
from core.hooks import Hook
import paths as home_paths

_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_HINT_BY_FORMAT = {
    "png": "image.png",
    "jpg": "image.jpg",
    "jpeg": "image.jpg",
    "webp": "image.webp",
    "gif": "image.gif",
}
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _safe_item_tag(item_id, index):
    if isinstance(item_id, str) and item_id.strip():
        clean = _SAFE_ID_RE.sub("_", item_id.strip()).strip("_")
        if clean:
            return clean[:32]
    return str(index + 1)


def _persist_hosted_image(
    raw_bytes,
    *,
    filename,
    media_type,
    session_attachments=None,
    attachments_dir=None,
    limits=None,
):
    if session_attachments is not None:
        tmp_dir = tempfile.mkdtemp(prefix="zero-hosted-img-")
        try:
            out_path = Path(tmp_dir) / filename
            out_path.write_bytes(raw_bytes)
            return session_attachments.add_file(
                out_path,
                name=filename,
                media_hint=media_type,
                delivery="reference",
                allow_over_limit=False,
                limits=limits,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    if attachments_dir is not None:
        folder = Path(attachments_dir)
        folder.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(raw_bytes).hexdigest()
        aid = f"att_{digest[:16]}"
        dest = folder / f"{digest[:8]}-{filename}"
        if not dest.exists():
            home_paths.write_whole(dest, raw_bytes)
        probed = attachment_metadata.probe_bytes(raw_bytes, media_type)
        meta = {
            "attachment_id": aid,
            "name": filename,
            "media_type": media_type,
            "size_bytes": len(raw_bytes),
            "sha256": digest,
            "path": str(dest),
            "metadata": probed,
        }
        att_entry = make_attachment_entry(
            aid,
            name=filename,
            media_type=media_type,
            size_bytes=len(raw_bytes),
            sha256=digest,
            path=str(dest),
            delivery="reference",
            metadata=probed,
        )
        return meta, att_entry
    return None, None


class HostedMediaHook(Hook):
    """Store completed hosted `image_generation_call` outputs as attachments.

    Decodes each completed hosted image once, attaches its `attachments/file`
    deliverable descriptor to the matching `model/hosted_output` entry, and
    strips raw base64 from private continuation so the blob is stored once and
    reconstructed on demand only when same-route replay requires it.
    """

    def assistant_entry(self, entry, session):
        children = entry.entries or ()
        if not children:
            return entry
        image_nodes = [
            (idx, child)
            for idx, child in enumerate(children)
            if child.type == "model/provider_item"
            and child.get("kind") == "response_item"
            and isinstance(child.get("item"), Mapping)
            and child.get("item").get("type") == "image_generation_call"
            and child.get("item").get("status") == "completed"
            and isinstance(child.get("item").get("result"), str)
            and bool(child.get("item").get("result"))
        ]
        if not image_nodes:
            return entry

        ctx = getattr(session, "tool_context", None) or {}
        session_attachments = ctx.get("session_attachments")
        attachments_dir = ctx.get("attachments_dir")
        if session_attachments is None and attachments_dir is None:
            return entry
        limits = ctx.get("limits")

        hosted_indices = [
            idx
            for idx, child in enumerate(children)
            if entries.has(child, "hosted_output")
            and child.get("kind") == "image_generation_call"
        ]
        new_children = list(children)
        changed = False
        matched_hosted = set()

        for order, (prov_idx, prov_node) in enumerate(image_nodes):
            item = dict(prov_node.get("item"))
            raw_b64 = item.get("result")
            try:
                raw_bytes = base64.b64decode(raw_b64, validate=True)
            except (binascii.Error, ValueError):
                continue
            if not raw_bytes:
                continue
            fmt = str(item.get("output_format") or "").strip().lower()
            hint_name = _HINT_BY_FORMAT.get(fmt, "image.png")
            media_type = attachment_metadata.media_type(hint_name, raw_bytes[:4096])
            if not media_type.startswith("image/"):
                media_type = "image/png"
            ext = _EXT_BY_MIME.get(media_type, ".png")
            item_id = item.get("id")
            filename = f"hosted-image-{_safe_item_tag(item_id, order)}{ext}"
            try:
                meta, att_entry = _persist_hosted_image(
                    raw_bytes,
                    filename=filename,
                    media_type=media_type,
                    session_attachments=session_attachments,
                    attachments_dir=attachments_dir,
                    limits=limits,
                )
            except (LimitExceeded, AttachmentError):
                continue
            if meta is None or att_entry is None:
                continue

            # Attach deliverable descriptor to the matching model/hosted_output.
            target_idx = None
            if isinstance(item_id, str) and item_id:
                for h_idx in hosted_indices:
                    if (
                        h_idx not in matched_hosted
                        and new_children[h_idx].get("item_id") == item_id
                    ):
                        target_idx = h_idx
                        break
            if target_idx is None:
                for h_idx in hosted_indices:
                    if h_idx not in matched_hosted:
                        target_idx = h_idx
                        break
            if target_idx is not None:
                matched_hosted.add(target_idx)
                hosted_node = new_children[target_idx]
                existing_kids = hosted_node.entries or ()
                if not any(
                    kid.type == ATTACHMENT_TYPE
                    and kid.get("attachment_id") == meta["attachment_id"]
                    for kid in existing_kids
                ):
                    new_children[target_idx] = replace(
                        hosted_node, entries=(*existing_kids, att_entry),
                    )

            # Strip raw base64 from private continuation and link by attachment_id.
            item.pop("result", None)
            prov_fields = {
                **(prov_node.fields or {}),
                "item": item,
                "attachment_id": meta["attachment_id"],
            }
            new_children[prov_idx] = replace(prov_node, fields=prov_fields)
            changed = True

        if not changed:
            return entry
        return replace(entry, entries=tuple(new_children))
