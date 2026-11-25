"""Content-addressed attachment blobs, staging leases and per-session hardlinks.

Every published blob lives at `state/attachments/blobs/<sha256[:2]>/<sha256>`
with mode 0400 and is published once via atomic `os.link`: an existing digest's
inode is never replaced, so per-session read-only hardlinks never orphan.
"""

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
import time
from pathlib import Path

from components.attachments import metadata as probe_metadata
from components.attachments.entries import ATTACHMENT_TYPE, METADATA_KEYS
import paths as home_paths

STAGING_LEASE_SECONDS = 24 * 3600
MAX_CHUNK_BYTES = 512 * 1024
MAX_LINK_NAME_BYTES = 180
MAX_SUFFIX_BYTES = 24
HASH_CHUNK_BYTES = 64 * 1024
HEAD_SNIFF_BYTES = 4096
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
UNSAFE_CHAR_RE = re.compile(r"[^A-Za-z0-9._-]+")


class AttachmentError(Exception):
    """An attachment operation was rejected."""


class LimitExceeded(AttachmentError):
    """A soft attachment size or store cap was exceeded."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS attachment_blobs (
    sha256 TEXT PRIMARY KEY,
    size_bytes INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,
    owner_key TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL
);

CREATE INDEX IF NOT EXISTS attachments_state_expires
    ON attachments(state, expires_at);

CREATE INDEX IF NOT EXISTS attachments_sha256
    ON attachments(sha256);

CREATE TABLE IF NOT EXISTS session_attachments (
    session TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    short_id TEXT NOT NULL,
    link_name TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (session, attachment_id),
    UNIQUE (session, link_name)
);

CREATE INDEX IF NOT EXISTS session_attachments_attachment
    ON session_attachments(attachment_id);

CREATE TABLE IF NOT EXISTS trashed_attachments (
    trash_id TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    trashed_at REAL NOT NULL,
    PRIMARY KEY (trash_id, attachment_id)
);

CREATE INDEX IF NOT EXISTS trashed_attachments_sha256
    ON trashed_attachments(sha256);
"""


def valid_session_id(sid):
    return isinstance(sid, str) and bool(sid) and SAFE_ID_RE.fullmatch(sid) is not None


def soft_cap_bytes(limits):
    """Return `(max_file_bytes, max_store_bytes)` from settings (0 = unlimited)."""
    if not limits:
        return None, None
    file_mb = limits.get("max_attachment_megabytes")
    store_mb = limits.get("max_attachment_store_megabytes")
    max_file = None if not file_mb else int(file_mb) * 1024 * 1024
    max_store = None if not store_mb else int(store_mb) * 1024 * 1024
    return max_file, max_store


def _truncate_utf8(text, max_bytes):
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # Trim without splitting a multi-byte UTF-8 sequence.
    cut = encoded[:max_bytes]
    while cut:
        try:
            return cut.decode("utf-8")
        except UnicodeDecodeError:
            cut = cut[:-1]
    return ""


def sanitize_filename(name):
    """Strip paths, control chars, and leading dots into a safe basename."""
    if not isinstance(name, str):
        return "file"
    # Take only the final path component across both POSIX and Windows separators.
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(ch for ch in base if ord(ch) >= 32 and ord(ch) != 127)
    cleaned = UNSAFE_CHAR_RE.sub("_", cleaned)
    cleaned = cleaned.lstrip(".")
    cleaned = cleaned.strip("_")
    if not cleaned or cleaned in (".", ".."):
        return "file"
    return cleaned


def safe_link_name(short_id, name):
    """Build `<short_id>-<sanitized_name>` strictly within 180 UTF-8 bytes."""
    prefix = f"{short_id}-"
    prefix_bytes = len(prefix.encode("utf-8"))
    budget = MAX_LINK_NAME_BYTES - prefix_bytes
    if budget < 1:
        raise ValueError("short_id exceeds hardlink filename budget")
    clean = sanitize_filename(name)
    if len(clean.encode("utf-8")) <= budget:
        return prefix + clean
    dot = clean.rfind(".")
    suffix = ""
    stem = clean
    if 0 < dot < len(clean) - 1:
        candidate = clean[dot:]
        if len(candidate.encode("utf-8")) <= min(MAX_SUFFIX_BYTES, budget - 1):
            suffix = candidate
            stem = clean[:dot]
    stem_budget = budget - len(suffix.encode("utf-8"))
    trimmed = _truncate_utf8(stem, stem_budget).rstrip("._") or "f"
    result = prefix + trimmed + suffix
    if len(result.encode("utf-8")) > MAX_LINK_NAME_BYTES:
        result = _truncate_utf8(result, MAX_LINK_NAME_BYTES)
    return result


def short_id_of(attachment_id):
    """Deterministic short prefix derived from an attachment ID."""
    raw = attachment_id.removeprefix("att_")
    token = re.sub(r"[^A-Za-z0-9]", "", raw)
    return (token[:8] or "att00000")


MIME_TOKEN_RE = re.compile(
    r"^[a-z0-9!#$%&'*+\-.^_`|~]{1,127}/[a-z0-9!#$%&'*+\-.^_`|~]{1,127}$"
)


def sanitize_media_type(value, *, default="application/octet-stream"):
    """Validate strict RFC MIME type/subtype syntax; reject CR/LF and controls."""
    if not isinstance(value, str) or not value:
        return default
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return default
    essence = value.split(";", 1)[0].strip().lower()
    if not essence or MIME_TOKEN_RE.fullmatch(essence) is None:
        return default
    return essence


def new_attachment_id():
    return f"att_{secrets.token_hex(8)}"


def _sync_dir(folder):
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class _PendingUpload:
    """In-flight chunked upload holding its staging temp file and byte reservation."""

    def __init__(self, upload_id, owner_key, name, media_hint, temp_path,
                 expected_size, allow_over_limit, created_at, expires_at):
        self.upload_id = upload_id
        self.owner_key = owner_key
        self.name = name
        self.media_hint = media_hint
        self.temp_path = temp_path
        self.expected_size = expected_size
        self.allow_over_limit = allow_over_limit
        self.created_at = created_at
        self.expires_at = expires_at
        self.received_bytes = 0
        self.reserved_bytes = 0
        self.head = bytearray()
        self.hasher = hashlib.sha256()
        self.closed = False
        self.lock = threading.Lock()


class AttachmentStore:
    """Filesystem blob store, upload staging, soft caps and hardlink materializer."""

    def __init__(self, root, *, lease_seconds=STAGING_LEASE_SECONDS):
        self.root = Path(root)
        self.blobs_dir = self.root / "blobs"
        self.staging_dir = self.root / "staging"
        self.sessions_dir = self.root / "sessions"
        self.lease_seconds = lease_seconds
        self._uploads_lock = threading.Lock()
        self._uploads = {}
        self._active_staging = set()
        self._reserve_lock = threading.Lock()
        self._blob_lock = threading.Lock()
        # Reserved bytes for concurrent non-chunked adds (e.g. attachment/add).
        self._ephemeral_reserved = 0

    def ensure_dirs(self):
        for folder in (self.root, self.blobs_dir, self.staging_dir, self.sessions_dir):
            home_paths.make_folders(folder)
            folder.chmod(0o700)

    def blob_path(self, sha256):
        if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
            raise AttachmentError("invalid attachment digest")
        return self.blobs_dir / sha256[:2] / sha256

    def session_dir(self, sid):
        if not valid_session_id(sid):
            raise AttachmentError(f"invalid session id {sid!r}")
        return self.sessions_dir / sid

    def ensure_session_dir(self, sid):
        folder = self.session_dir(sid)
        home_paths.make_folders(folder)
        folder.chmod(0o700)
        return folder

    def remove_session_dir(self, sid):
        if not valid_session_id(sid):
            return
        folder = self.sessions_dir / sid
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)

    def publish_blob(self, source_path, sha256):
        """Publish `source_path` immutably at `blobs/<sha[:2]>/<sha>` with 0400.

        Uses atomic `os.link` if absent and NEVER replaces an existing digest's
        inode so existing hardlinks never orphan. Always removes `source_path`.
        """
        target = self.blob_path(sha256)
        shard = target.parent
        home_paths.make_folders(shard)
        shard.chmod(0o700)
        tmp_path = None
        try:
            # Ensure temp file sits in the same shard directory for atomic os.link.
            if Path(source_path).parent == shard:
                tmp_path = Path(source_path)
            else:
                fd, raw_tmp = tempfile.mkstemp(prefix=".pub-", dir=shard)
                tmp_path = Path(raw_tmp)
                try:
                    with os.fdopen(fd, "wb") as dst, open(source_path, "rb") as src:
                        shutil.copyfileobj(src, dst, HASH_CHUNK_BYTES)
                        dst.flush()
                        os.fsync(dst.fileno())
                except BaseException:
                    tmp_path.unlink(missing_ok=True)
                    raise
            os.chmod(tmp_path, 0o400)
            try:
                os.link(tmp_path, target)
                _sync_dir(shard)
                created = True
            except FileExistsError:
                created = False
            return target, created
        finally:
            if tmp_path is not None and tmp_path != target:
                tmp_path.unlink(missing_ok=True)
            if Path(source_path) != target and Path(source_path) != tmp_path:
                Path(source_path).unlink(missing_ok=True)

    def materialize_session_link(self, sid, sha256, link_name):
        """Create a read-only hardlink inside the session's attachments folder."""
        blob = self.blob_path(sha256)
        if not blob.is_file():
            return None
        folder = self.ensure_session_dir(sid)
        target = folder / link_name
        try:
            os.link(blob, target)
        except FileExistsError:
            if not os.path.samefile(blob, target):
                raise AttachmentError(
                    f"conflicting link name {link_name!r} in session {sid}"
                ) from None
        return target

    def _total_reserved_unlocked(self):
        return (
            sum(item.reserved_bytes for item in self._uploads.values())
            + self._ephemeral_reserved
        )

    def _check_caps(self, store_db, *, file_bytes, added_store_bytes,
                    max_file_bytes, max_store_bytes, allow_over_limit,
                    gc_callback=None):
        if allow_over_limit:
            return
        if max_file_bytes is not None and file_bytes > max_file_bytes:
            limit_mb = max_file_bytes / (1024 * 1024)
            raise LimitExceeded(
                f"attachment size ({file_bytes} bytes) exceeds soft limit "
                f"({limit_mb:g} MiB); set allow_over_limit to override"
            )
        if max_store_bytes is None or added_store_bytes <= 0:
            return
        used = self.stored_blob_bytes(store_db)
        reserved = self._total_reserved_unlocked()
        if used + reserved + added_store_bytes > max_store_bytes:
            # Opportunistically reclaim expired items before failing soft store cap.
            if gc_callback is not None:
                gc_callback()
                used = self.stored_blob_bytes(store_db)
            if used + reserved + added_store_bytes > max_store_bytes:
                limit_mb = max_store_bytes / (1024 * 1024)
                raise LimitExceeded(
                    f"attachment store limit ({limit_mb:g} MiB) would be exceeded; "
                    f"prune unused attachments or set allow_over_limit to override"
                )

    @staticmethod
    def stored_blob_bytes(store_db):
        rows = store_db.query(
            "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM attachment_blobs"
        )
        return int(rows[0]["total"]) if rows else 0

    def _register_staged(self, store_db, *, sha256, size_bytes, name, media_type,
                         metadata_dict, owner_key, now):
        attachment_id = new_attachment_id()
        expires_at = now + self.lease_seconds
        meta_json = json.dumps(metadata_dict, sort_keys=True)

        def change(db):
            db.execute(
                "INSERT OR IGNORE INTO attachment_blobs "
                "(sha256, size_bytes, media_type, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (sha256, size_bytes, media_type, meta_json, now),
            )
            db.execute(
                "INSERT INTO attachments "
                "(id, sha256, name, media_type, size_bytes, metadata, state, "
                "owner_key, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'staged', ?, ?, ?)",
                (
                    attachment_id, sha256, name, media_type, size_bytes,
                    meta_json, owner_key, now, expires_at,
                ),
            )

        store_db.write(change)
        return {
            "attachment_id": attachment_id,
            "sha256": sha256,
            "name": name,
            "media_type": media_type,
            "size_bytes": size_bytes,
            "state": "staged",
            "created_at": now,
            "expires_at": expires_at,
            "metadata": metadata_dict,
            "link_name": None,
            "path": None,
        }

    def add_host_file(self, store_db, path, *, owner_key, name=None,
                      media_hint=None, max_file_bytes=None,
                      max_store_bytes=None, allow_over_limit=False,
                      gc_callback=None):
        """Snapshot a local regular file into the immutable blob store."""
        self.ensure_dirs()
        src = Path(path)
        if not src.is_absolute():
            raise AttachmentError(f"attachment path must be absolute: {path!r}")
        try:
            st = src.stat()
        except OSError as exc:
            raise AttachmentError(f"cannot read {path!r}: {exc}") from None
        if not stat.S_ISREG(st.st_mode):
            raise AttachmentError(f"attachment path must be a regular file: {path!r}")
        display_name = name or src.name or "file"
        size_est = st.st_size
        reserved = 0
        with self._reserve_lock:
            self._check_caps(
                store_db,
                file_bytes=size_est,
                added_store_bytes=size_est,
                max_file_bytes=max_file_bytes,
                max_store_bytes=max_store_bytes,
                allow_over_limit=allow_over_limit,
                gc_callback=gc_callback,
            )
            self._ephemeral_reserved += size_est
            reserved = size_est
        with self._uploads_lock:
            fd, raw_tmp = tempfile.mkstemp(prefix="add-", dir=self.staging_dir)
            tmp_path = Path(raw_tmp)
            self._active_staging.add(tmp_path)
        try:
            hasher = hashlib.sha256()
            head = bytearray()
            total = 0
            with os.fdopen(fd, "wb") as dst, open(src, "rb") as reader:
                while chunk := reader.read(HASH_CHUNK_BYTES):
                    total += len(chunk)
                    if (not allow_over_limit and max_file_bytes is not None
                            and total > max_file_bytes):
                        limit_mb = max_file_bytes / (1024 * 1024)
                        raise LimitExceeded(
                            f"attachment size exceeds soft limit ({limit_mb:g} MiB); "
                            "set allow_over_limit to override"
                        )
                    if len(head) < HEAD_SNIFF_BYTES:
                        need = HEAD_SNIFF_BYTES - len(head)
                        head.extend(chunk[:need])
                    hasher.update(chunk)
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            sha256 = hasher.hexdigest()
            return self._finalize_temp_file(
                store_db,
                tmp_path=tmp_path,
                sha256=sha256,
                size_bytes=total,
                head=bytes(head),
                name=display_name,
                media_hint=media_hint,
                owner_key=owner_key,
                max_file_bytes=max_file_bytes,
                max_store_bytes=max_store_bytes,
                allow_over_limit=allow_over_limit,
                reserved_to_release=reserved,
                gc_callback=gc_callback,
            )
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            with self._reserve_lock:
                self._ephemeral_reserved = max(0, self._ephemeral_reserved - reserved)
            raise
        finally:
            with self._uploads_lock:
                self._active_staging.discard(tmp_path)

    def _finalize_temp_file(self, store_db, *, tmp_path, sha256, size_bytes,
                            head, name, media_hint, owner_key,
                            max_file_bytes, max_store_bytes, allow_over_limit,
                            reserved_to_release=0, upload_id_to_pop=None,
                            gc_callback=None):
        detected = sanitize_media_type(probe_metadata.media_type(name, head))
        if detected == "application/octet-stream" and media_hint:
            detected = sanitize_media_type(media_hint)
        probed = probe_metadata.probe(tmp_path, detected)
        if not isinstance(probed, dict):
            probed = {}
        clean_meta = {k: probed[k] for k in METADATA_KEYS if k in probed}
        now = time.time()
        with self._reserve_lock:
            existing = store_db.query(
                "SELECT sha256 FROM attachment_blobs WHERE sha256 = ?", sha256
            )
            # Release our own reservation before checking net unique delta.
            if reserved_to_release:
                self._ephemeral_reserved = max(
                    0, self._ephemeral_reserved - reserved_to_release
                )
            if upload_id_to_pop is not None:
                with self._uploads_lock:
                    popped = self._uploads.pop(upload_id_to_pop, None)
                    if popped is not None:
                        popped.reserved_bytes = 0
            added_bytes = 0 if existing else size_bytes
            self._check_caps(
                store_db,
                file_bytes=size_bytes,
                added_store_bytes=added_bytes,
                max_file_bytes=max_file_bytes,
                max_store_bytes=max_store_bytes,
                allow_over_limit=allow_over_limit,
                gc_callback=gc_callback,
            )
            # Synchronize blob publication + DB registration against prune unlink.
            with self._blob_lock:
                self.publish_blob(tmp_path, sha256)
                return self._register_staged(
                    store_db,
                    sha256=sha256,
                    size_bytes=size_bytes,
                    name=name,
                    media_type=detected,
                    metadata_dict=clean_meta,
                    owner_key=owner_key,
                    now=now,
                )

    def upload_chunk(self, store_db, *, owner_key, data_bytes, upload_id=None,
                     name=None, media_hint=None, expected_size=None,
                     offset=0, final=True, abort=False,
                     max_file_bytes=None, max_store_bytes=None,
                     allow_over_limit=False, gc_callback=None):
        """Handle one chunk (<= 512 KiB) of a staged upload."""
        self.ensure_dirs()
        if len(data_bytes) > MAX_CHUNK_BYTES:
            raise AttachmentError(
                f"upload chunk ({len(data_bytes)} bytes) exceeds "
                f"{MAX_CHUNK_BYTES} bytes"
            )
        if abort:
            if not upload_id:
                raise AttachmentError("abort requires upload_id")
            return self._abort_upload(upload_id, owner_key)
        now = time.time()
        if upload_id is None:
            if not isinstance(name, str) or not name.strip():
                raise AttachmentError("attachment/upload requires a file name")
            if offset != 0:
                raise AttachmentError("initial upload chunk must start at offset 0")
            if expected_size is not None and (
                type(expected_size) is not int or expected_size < 0
            ):
                raise AttachmentError("expected_size must be a nonnegative integer")
            new_uid = f"upl_{secrets.token_hex(8)}"
            initial_reserve = (
                expected_size if expected_size is not None else len(data_bytes)
            )
            with self._reserve_lock:
                self._check_caps(
                    store_db,
                    file_bytes=max(initial_reserve, len(data_bytes)),
                    added_store_bytes=initial_reserve,
                    max_file_bytes=max_file_bytes,
                    max_store_bytes=max_store_bytes,
                    allow_over_limit=allow_over_limit,
                    gc_callback=gc_callback,
                )
                fd, raw_tmp = tempfile.mkstemp(prefix="upl-", dir=self.staging_dir)
                os.close(fd)
                upload = _PendingUpload(
                    new_uid, owner_key, name, media_hint, Path(raw_tmp),
                    expected_size, allow_over_limit, now,
                    now + self.lease_seconds,
                )
                upload.reserved_bytes = initial_reserve
                with self._uploads_lock:
                    self._uploads[new_uid] = upload
        else:
            with self._uploads_lock:
                upload = self._uploads.get(upload_id)
            if upload is None:
                raise AttachmentError(f"unknown or completed upload {upload_id!r}")
            if upload.owner_key != owner_key:
                raise AttachmentError("upload belongs to another caller")
            if upload.expires_at <= now:
                self._abort_upload(upload_id, owner_key)
                raise AttachmentError("upload lease expired")
        with upload.lock:
            if upload.closed:
                raise AttachmentError(
                    f"unknown or completed upload {upload.upload_id!r}"
                )
            if offset != upload.received_bytes:
                raise AttachmentError(
                    f"upload offset mismatch: expected {upload.received_bytes}, "
                    f"got {offset}"
                )
            new_total = upload.received_bytes + len(data_bytes)
            extra_reserve = max(0, new_total - upload.reserved_bytes)
            effective_override = upload.allow_over_limit or allow_over_limit
            if extra_reserve or (not effective_override and max_file_bytes is not None
                                 and new_total > max_file_bytes):
                with self._reserve_lock:
                    self._check_caps(
                        store_db,
                        file_bytes=new_total,
                        added_store_bytes=extra_reserve,
                        max_file_bytes=max_file_bytes,
                        max_store_bytes=max_store_bytes,
                        allow_over_limit=effective_override,
                        gc_callback=gc_callback,
                    )
                    upload.reserved_bytes += extra_reserve
            if data_bytes:
                with open(upload.temp_path, "ab") as stream:
                    stream.write(data_bytes)
                if len(upload.head) < HEAD_SNIFF_BYTES:
                    need = HEAD_SNIFF_BYTES - len(upload.head)
                    upload.head.extend(data_bytes[:need])
                upload.hasher.update(data_bytes)
                upload.received_bytes = new_total
            if not final:
                return {
                    "upload_id": upload.upload_id,
                    "received_bytes": upload.received_bytes,
                    "complete": False,
                }
            # Finalize: flush, publish, and register staged attachment row.
            try:
                with open(upload.temp_path, "ab") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
                attachment = self._finalize_temp_file(
                    store_db,
                    tmp_path=upload.temp_path,
                    sha256=upload.hasher.hexdigest(),
                    size_bytes=upload.received_bytes,
                    head=bytes(upload.head),
                    name=upload.name,
                    media_hint=upload.media_hint,
                    owner_key=upload.owner_key,
                    max_file_bytes=max_file_bytes,
                    max_store_bytes=max_store_bytes,
                    allow_over_limit=effective_override,
                    upload_id_to_pop=upload.upload_id,
                    gc_callback=gc_callback,
                )
            except BaseException:
                upload.temp_path.unlink(missing_ok=True)
                with self._uploads_lock:
                    self._uploads.pop(upload.upload_id, None)
                upload.reserved_bytes = 0
                raise
            finally:
                upload.closed = True
            return {
                "upload_id": upload.upload_id,
                "received_bytes": upload.received_bytes,
                "complete": True,
                "attachment": attachment,
            }

    def _abort_upload(self, upload_id, owner_key):
        with self._uploads_lock:
            upload = self._uploads.get(upload_id)
            if upload is None:
                raise AttachmentError(f"unknown upload {upload_id!r}")
            if upload.owner_key != owner_key:
                raise AttachmentError("upload belongs to another caller")
            self._uploads.pop(upload_id, None)
        # Acquire upload.lock after releasing _uploads_lock (lock-order safe).
        with upload.lock:
            upload.closed = True
            upload.reserved_bytes = 0
            upload.temp_path.unlink(missing_ok=True)
        return {"upload_id": upload_id, "aborted": True}

    def expire_uploads(self, now=None):
        """Drop expired in-flight uploads and orphan staging temp files."""
        now = time.time() if now is None else now
        expired = 0
        removed = []
        with self._uploads_lock:
            stale = [
                uid for uid, item in self._uploads.items()
                if item.expires_at <= now
            ]
            for uid in stale:
                removed.append(self._uploads.pop(uid))
            active_paths = (
                {item.temp_path for item in self._uploads.values()}
                | set(self._active_staging)
            )
        for item in removed:
            with item.lock:
                item.closed = True
                item.reserved_bytes = 0
                item.temp_path.unlink(missing_ok=True)
                expired += 1
        if self.staging_dir.is_dir():
            with self._uploads_lock:
                active_paths = (
                    {item.temp_path for item in self._uploads.values()}
                    | set(self._active_staging)
                )
                for path in self.staging_dir.iterdir():
                    if path in active_paths or not path.is_file():
                        continue
                    try:
                        age = now - path.stat().st_mtime
                    except OSError:
                        continue
                    if age >= self.lease_seconds:
                        path.unlink(missing_ok=True)
                        expired += 1
        return expired

    def read_chunk(self, sha256, *, offset=0, length=MAX_CHUNK_BYTES):
        """Read up to 512 KiB of exact unmodified blob bytes."""
        if type(offset) is not int or offset < 0:
            raise AttachmentError("offset must be a nonnegative integer")
        if type(length) is not int or not 1 <= length <= MAX_CHUNK_BYTES:
            raise AttachmentError(
                f"length must be an integer between 1 and {MAX_CHUNK_BYTES}"
            )
        path = self.blob_path(sha256)
        if not path.is_file():
            raise AttachmentError("attachment content is missing from store")
        total = path.stat().st_size
        if offset > total:
            return b"", total, True
        with open(path, "rb") as stream:
            stream.seek(offset)
            chunk = stream.read(length)
        eof = (offset + len(chunk)) >= total
        return chunk, total, eof

    def prune(self, store_db, *, now=None, include_trashed=False,
              live_sessions_dir=None):
        """Expire stale staging leases and delete unreferenced blobs."""
        now = time.time() if now is None else now
        self.expire_uploads(now)
        removed_blobs = []
        stats = {"expired_staged": 0, "trashed_pruned": 0}

        def change(db):
            expired_rows = db.execute(
                "SELECT id FROM attachments "
                "WHERE state = 'staged' AND expires_at IS NOT NULL "
                "AND expires_at <= ?",
                (now,),
            ).fetchall()
            stats["expired_staged"] = len(expired_rows)
            if expired_rows:
                db.executemany(
                    "DELETE FROM attachments WHERE id = ?",
                    [(row["id"],) for row in expired_rows],
                )
            if include_trashed:
                trashed = db.execute(
                    "SELECT trash_id, attachment_id FROM trashed_attachments"
                ).fetchall()
                to_drop = []
                for row in trashed:
                    if live_sessions_dir is not None and (
                        Path(live_sessions_dir) / f"{row['trash_id']}.jsonl"
                    ).exists():
                        continue
                    to_drop.append((row["trash_id"], row["attachment_id"]))
                stats["trashed_pruned"] = len(to_drop)
                if to_drop:
                    db.executemany(
                        "DELETE FROM trashed_attachments "
                        "WHERE trash_id = ? AND attachment_id = ?",
                        to_drop,
                    )
            # Remove claimed attachment rows that have neither live nor trashed refs.
            db.execute(
                "DELETE FROM attachments WHERE state != 'staged' "
                "AND id NOT IN (SELECT attachment_id FROM session_attachments) "
                "AND id NOT IN (SELECT attachment_id FROM trashed_attachments)"
            )
            orphans = db.execute(
                "SELECT sha256, size_bytes FROM attachment_blobs "
                "WHERE sha256 NOT IN (SELECT sha256 FROM attachments) "
                "AND sha256 NOT IN (SELECT sha256 FROM trashed_attachments)"
            ).fetchall()
            for row in orphans:
                removed_blobs.append((row["sha256"], int(row["size_bytes"])))
            if orphans:
                db.executemany(
                    "DELETE FROM attachment_blobs WHERE sha256 = ?",
                    [(row["sha256"],) for row in orphans],
                )

        freed_bytes = 0
        # Hold _blob_lock across orphan DB deletion and disk unlink so a
        # concurrent upload of the same digest cannot reuse an about-to-unlink inode.
        with self._blob_lock:
            store_db.write(change)
            for sha256, size_bytes in removed_blobs:
                try:
                    path = self.blob_path(sha256)
                    path.unlink(missing_ok=True)
                    freed_bytes += size_bytes
                    # Clean empty shard directory opportunistically.
                    try:
                        path.parent.rmdir()
                    except OSError:
                        pass
                except AttachmentError:
                    pass
        return {
            "expired_staged": stats["expired_staged"],
            "removed_blobs": len(removed_blobs),
            "freed_bytes": freed_bytes,
            "trashed_pruned": stats["trashed_pruned"],
        }


def collect_record_attachments(records):
    """Extract unique `(attachment_id, node)` pairs referenced in `records`."""
    from core import entries

    found = []
    seen = set()
    for record in records:
        for node, _, _, _ in entries.walk(record.entry):
            if node.type != ATTACHMENT_TYPE:
                continue
            aid = node.get("attachment_id")
            if isinstance(aid, str) and aid and aid not in seen:
                seen.add(aid)
                found.append((aid, node))
    return found
