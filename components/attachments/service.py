"""Server service for content-addressed attachments, leases, auth and GC."""

import base64
import binascii
from collections.abc import Mapping
from dataclasses import replace
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

from core import entries as core_entries
from components.attachments.entries import (
    ATTACHMENT_TYPE,
    METADATA_KEYS,
    attachment_ids,
    attachment_label,
    make_attachment_entry,
)
from components.attachments.store import (
    AttachmentError,
    AttachmentStore,
    LimitExceeded,
    MAX_CHUNK_BYTES,
    SCHEMA,
    STAGING_LEASE_SECONDS,
    collect_record_attachments,
    safe_link_name,
    short_id_of,
    valid_session_id,
)
from server.services import PreparedMessage, Principal, Refused, Service


_ATTACHMENTS_SECTION_RE = re.compile(
    r"(?ms)^## Attachments[ \t]*(?:\n.*?)?(?=^## |\Z)",
)


def _format_boundary_attachments_section(summary, attachments):
    """Regenerate a compaction boundary's `## Attachments` block deterministically."""
    if not attachments:
        return summary
    lines = []
    for item in attachments:
        aid = item["attachment_id"]
        name = item.get("name") or aid
        media = item.get("media_type") or "application/octet-stream"
        path = item.get("path")
        if path:
            lines.append(f"- {aid}: {name} ({media}) at {path}")
        else:
            lines.append(f"- {aid}: {name} ({media})")
    cleaned = _ATTACHMENTS_SECTION_RE.sub("", summary or "").rstrip()
    section = "## Attachments\n" + "\n".join(lines)
    return f"{cleaned}\n\n{section}" if cleaned else section


def _megabytes_to_bytes(value):
    if value is None or value == 0:
        return None
    return int(value) * 1024 * 1024


def _format_meta(row, *, link_name=None, path=None):
    raw_meta = row.get("metadata")
    if isinstance(raw_meta, str):
        try:
            meta = json.loads(raw_meta)
        except ValueError:
            meta = {}
    elif isinstance(raw_meta, dict):
        meta = dict(raw_meta)
    else:
        meta = {}
    return {
        "attachment_id": row["id"],
        "sha256": row["sha256"],
        "name": row["name"],
        "media_type": row["media_type"],
        "size_bytes": int(row["size_bytes"]),
        "state": row["state"],
        "created_at": float(row["created_at"]),
        "expires_at": (
            float(row["expires_at"]) if row.get("expires_at") is not None else None
        ),
        "metadata": meta,
        "link_name": link_name,
        "path": path,
    }


def _unique_link_name(db, sid, attachment_id, name):
    base_short = short_id_of(attachment_id)
    for attempt in range(1, 100):
        short = base_short if attempt == 1 else f"{base_short}{attempt}"
        candidate = safe_link_name(short, name)
        taken = db.execute(
            "SELECT 1 FROM session_attachments WHERE session = ? AND link_name = ?",
            (sid, candidate),
        ).fetchone()
        if not taken:
            return short, candidate
    raise Refused(f"could not allocate unique attachment filename for {name!r}")


class SessionAttachments:
    """Session-scoped attachment resolver and snapshot helper for tools."""

    def __init__(self, service, sid):
        self.service = service
        self.sid = sid

    def resolve(self, attachment_id):
        """Return metadata and readable mount path for a session attachment."""
        if not isinstance(attachment_id, str) or not attachment_id:
            return None
        rows = self.service.runtime.store.query(
            "SELECT a.*, sa.link_name "
            "FROM session_attachments sa "
            "JOIN attachments a ON a.id = sa.attachment_id "
            "WHERE sa.session = ? AND sa.attachment_id = ?",
            self.sid, attachment_id,
        )
        if not rows:
            return None
        row = rows[0]
        link_name = row["link_name"]
        self.service.store.materialize_session_link(
            self.sid, row["sha256"], link_name,
        )
        path = str(self.service.store.session_dir(self.sid) / link_name)
        return _format_meta(row, link_name=link_name, path=path)

    def over_limit_reason(self, file_bytes, *, limits=None):
        """Return a factual reason if snapshotting `file_bytes` exceeds soft caps."""
        if limits is not None:
            max_file = _megabytes_to_bytes(limits.get("max_attachment_megabytes"))
            max_store = _megabytes_to_bytes(
                limits.get("max_attachment_store_megabytes")
            )
        else:
            max_file, max_store = self.service._limits()
        if max_file is not None and file_bytes > max_file:
            limit_mb = max_file / (1024 * 1024)
            return (
                f"file size ({file_bytes:,} bytes) exceeds soft limit "
                f"({limit_mb:g} MiB)"
            )
        if max_store is not None and file_bytes > 0:
            used = self.service.store.stored_blob_bytes(self.service.runtime.store)
            if used + file_bytes > max_store:
                limit_mb = max_store / (1024 * 1024)
                return (
                    f"attachment store limit ({limit_mb:g} MiB) would be exceeded"
                )
        return None

    def add_file(
        self,
        path,
        *,
        name=None,
        media_hint=None,
        delivery="inline",
        allow_over_limit=False,
        limits=None,
    ):
        """Snapshot a local file into the store and claim it for this session."""
        if limits is not None:
            max_file = _megabytes_to_bytes(limits.get("max_attachment_megabytes"))
            max_store = _megabytes_to_bytes(
                limits.get("max_attachment_store_megabytes")
            )
        else:
            max_file, max_store = self.service._limits()
        staged = self.service.store.add_host_file(
            self.service.runtime.store,
            path,
            owner_key=f"session:{self.sid}",
            name=name,
            media_hint=media_hint,
            max_file_bytes=max_file,
            max_store_bytes=max_store,
            allow_over_limit=allow_over_limit,
            gc_callback=self.service._auto_gc,
        )
        aid = staged["attachment_id"]
        now = time.time()
        chosen = {}

        def claim(db):
            db.execute(
                "UPDATE attachments SET state = 'claimed', expires_at = NULL "
                "WHERE id = ?",
                (aid,),
            )
            short, link_name = _unique_link_name(
                db, self.sid, aid, staged["name"],
            )
            db.execute(
                "INSERT INTO session_attachments "
                "(session, attachment_id, short_id, link_name, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (self.sid, aid, short, link_name, now),
            )
            chosen["link_name"] = link_name

        self.service.runtime.store.write(claim)
        self.service._mark_session_has_attachments(self.sid)
        link_name = chosen["link_name"]
        self.service.store.materialize_session_link(
            self.sid, staged["sha256"], link_name,
        )
        link_path = str(self.service.store.session_dir(self.sid) / link_name)
        meta = {
            **staged,
            "state": "claimed",
            "expires_at": None,
            "link_name": link_name,
            "path": link_path,
        }
        entry = make_attachment_entry(
            aid,
            name=meta["name"],
            media_type=meta["media_type"],
            size_bytes=meta["size_bytes"],
            sha256=meta["sha256"],
            path=link_path,
            delivery=delivery,
            metadata=meta.get("metadata"),
        )
        return meta, entry


class AttachmentsService(Service):
    name = "attachments"
    schema = SCHEMA
    tables = ("session_attachments",)
    features = ("attachments",)
    looking_messages = ("attachment/read", "attachment/get", "attachment/list")
    principal_messages = (
        "attachment/upload",
        "attachment/add",
        "attachment/read",
        "attachment/get",
        "attachment/list",
        "attachment/prune",
    )
    message_fields = ("attachments",)

    def __init__(self, *, lease_seconds=STAGING_LEASE_SECONDS):
        self.lease_seconds = lease_seconds
        self.store = None
        self._sessions_lock = threading.Lock()
        self._sessions_with_attachments = set()

    def attach(self, runtime):
        super().attach(runtime)
        self.store = AttachmentStore(
            runtime.paths.state / "attachments",
            lease_seconds=self.lease_seconds,
        )
        self._refresh_sessions_index()

    def begin(self):
        self.store.ensure_dirs()
        self._refresh_sessions_index()
        self._auto_gc()

    def _refresh_sessions_index(self):
        try:
            rows = self.runtime.store.query(
                "SELECT DISTINCT session FROM session_attachments",
            )
        except sqlite3.OperationalError:
            return
        with self._sessions_lock:
            self._sessions_with_attachments = {row["session"] for row in rows}

    def _mark_session_has_attachments(self, sid):
        with self._sessions_lock:
            self._sessions_with_attachments.add(sid)

    def _unmark_session_has_attachments(self, sid):
        with self._sessions_lock:
            self._sessions_with_attachments.discard(sid)

    def _session_has_attachments(self, sid):
        with self._sessions_lock:
            return sid in self._sessions_with_attachments

    def _limits(self):
        cfg = self.runtime.config()
        file_mb = cfg.get("max_attachment_megabytes")
        store_mb = cfg.get("max_attachment_store_megabytes")
        return _megabytes_to_bytes(file_mb), _megabytes_to_bytes(store_mb)

    def _auto_gc(self):
        try:
            self.store.prune(
                self.runtime.store,
                live_sessions_dir=self.runtime.paths.sessions,
            )
        except Exception:
            pass

    def messages(self):
        return {
            "attachment/upload": self.upload,
            "attachment/add": self.add,
            "attachment/read": self.read,
            "attachment/get": self.get,
            "attachment/list": self.list,
            "attachment/prune": self.prune,
        }

    def _check_session_access(self, sid, principal):
        if not valid_session_id(sid):
            raise Refused(f"invalid session id {sid!r}")
        if principal.scope is not None:
            if not self.runtime.in_workflow_scope(sid, principal.scope):
                raise Refused("session is outside workflow scope")
        elif principal.command_session is not None:
            if sid != principal.command_session:
                raise Refused("ZERO command may only access its own session")
        elif principal.is_command:
            raise Refused("ZERO command cannot access session attachments")

    def _authorize_read(self, attachment_id, principal, session=None):
        if not isinstance(attachment_id, str) or not attachment_id:
            raise Refused("attachment_id must be a nonempty string")
        rows = self.runtime.store.query(
            "SELECT * FROM attachments WHERE id = ?", attachment_id,
        )
        if not rows:
            raise Refused(f"unknown attachment {attachment_id!r}")
        row = rows[0]
        now = time.time()
        sa_rows = self.runtime.store.query(
            "SELECT * FROM session_attachments WHERE attachment_id = ?",
            attachment_id,
        )
        if session is not None:
            self._check_session_access(session, principal)
        if principal.scope is not None:
            if row["state"] == "staged":
                if row["owner_key"] != principal.owner_key:
                    raise Refused("attachment belongs to another caller")
                if row["expires_at"] is not None and row["expires_at"] <= now:
                    raise Refused("staged attachment lease has expired")
                if session is not None:
                    raise Refused("staged attachment is not attached to that session")
                return row, None
            allowed = [
                sa for sa in sa_rows
                if self.runtime.in_workflow_scope(sa["session"], principal.scope)
                and (session is None or sa["session"] == session)
            ]
            if not allowed:
                raise Refused("attachment is outside workflow scope")
            return row, allowed[0] if session is not None else None
        if principal.is_command or principal.command_session is not None:
            if principal.command_session is None:
                raise Refused("ZERO command cannot read attachments")
            matched = [
                sa for sa in sa_rows if sa["session"] == principal.command_session
            ]
            if not matched:
                raise Refused("attachment does not belong to this command's session")
            return row, matched[0]
        # User client (local or relayed).
        if row["state"] == "staged":
            if row["owner_key"] != principal.owner_key:
                raise Refused("staged attachment belongs to another caller")
            if row["expires_at"] is not None and row["expires_at"] <= now:
                raise Refused("staged attachment lease has expired")
            if session is not None:
                raise Refused("staged attachment is not attached to that session")
            return row, None
        if not sa_rows:
            raise Refused("attachment is no longer accessible")
        if session is not None:
            matched = [sa for sa in sa_rows if sa["session"] == session]
            if not matched:
                raise Refused("attachment is not attached to that session")
            return row, matched[0]
        return row, None

    def upload(self, request, principal=None):
        principal = principal or Principal()
        if principal.is_command or principal.command_session is not None:
            raise Refused("ZERO commands cannot upload attachments")
        abort = request.get("abort", False)
        if not isinstance(abort, bool):
            raise Refused("abort must be true or false")
        upload_id = request.get("upload_id")
        if upload_id is not None and not isinstance(upload_id, str):
            raise Refused("upload_id must be a string")
        name = request.get("name")
        if name is not None and not isinstance(name, str):
            raise Refused("name must be a string")
        media_hint = request.get("media_type")
        if media_hint is not None and not isinstance(media_hint, str):
            raise Refused("media_type must be a string")
        expected_size = request.get("expected_size")
        offset = request.get("offset", 0)
        if type(offset) is not int or offset < 0:
            raise Refused("offset must be a nonnegative integer")
        final = request.get("final", True)
        if not isinstance(final, bool):
            raise Refused("final must be true or false")
        allow_over_limit = request.get("allow_over_limit", False)
        if not isinstance(allow_over_limit, bool):
            raise Refused("allow_over_limit must be true or false")
        raw_data = request.get("data", "")
        if not isinstance(raw_data, str):
            raise Refused("data must be a base64 string")
        if abort:
            data_bytes = b""
        else:
            try:
                data_bytes = base64.b64decode(raw_data, validate=True)
            except (binascii.Error, ValueError):
                raise Refused("data must be valid base64") from None
        max_file, max_store = self._limits()
        try:
            result = self.store.upload_chunk(
                self.runtime.store,
                owner_key=principal.owner_key,
                data_bytes=data_bytes,
                upload_id=upload_id,
                name=name,
                media_hint=media_hint,
                expected_size=expected_size,
                offset=offset,
                final=final,
                abort=abort,
                max_file_bytes=max_file,
                max_store_bytes=max_store,
                allow_over_limit=allow_over_limit,
                gc_callback=self._auto_gc,
            )
        except LimitExceeded as exc:
            return {"ok": False, "error": str(exc), "over_limit": True}
        except AttachmentError as exc:
            raise Refused(str(exc)) from None
        return {"ok": True, **result}

    def add(self, request, principal=None):
        principal = principal or Principal()
        if not principal.can_add_host_path:
            raise Refused(
                "attachment/add reads a local host path and is only permitted "
                "from a direct local user client"
            )
        path = request.get("path")
        if not isinstance(path, str) or not path:
            raise Refused("attachment/add requires a file path")
        name = request.get("name")
        if name is not None and not isinstance(name, str):
            raise Refused("name must be a string")
        media_hint = request.get("media_type")
        if media_hint is not None and not isinstance(media_hint, str):
            raise Refused("media_type must be a string")
        allow_over_limit = request.get("allow_over_limit", False)
        if not isinstance(allow_over_limit, bool):
            raise Refused("allow_over_limit must be true or false")
        max_file, max_store = self._limits()
        try:
            attachment = self.store.add_host_file(
                self.runtime.store,
                path,
                owner_key=principal.owner_key,
                name=name,
                media_hint=media_hint,
                max_file_bytes=max_file,
                max_store_bytes=max_store,
                allow_over_limit=allow_over_limit,
                gc_callback=self._auto_gc,
            )
        except LimitExceeded as exc:
            return {"ok": False, "error": str(exc), "over_limit": True}
        except AttachmentError as exc:
            raise Refused(str(exc)) from None
        return {"ok": True, "attachment": attachment}

    def read(self, request, principal=None):
        principal = principal or Principal()
        attachment_id = request.get("attachment_id")
        session = request.get("session")
        if session is not None and not isinstance(session, str):
            raise Refused("session must be a string")
        offset = request.get("offset", 0)
        length = request.get("length", MAX_CHUNK_BYTES)
        row, _ = self._authorize_read(attachment_id, principal, session=session)
        try:
            chunk, total, eof = self.store.read_chunk(
                row["sha256"], offset=offset, length=length,
            )
        except AttachmentError as exc:
            raise Refused(str(exc)) from None
        return {
            "ok": True,
            "attachment_id": row["id"],
            "offset": offset,
            "data": base64.b64encode(chunk).decode("ascii"),
            "bytes_read": len(chunk),
            "size_bytes": total,
            "eof": eof,
        }

    def get(self, request, principal=None):
        principal = principal or Principal()
        attachment_id = request.get("attachment_id")
        session = request.get("session")
        if session is not None and not isinstance(session, str):
            raise Refused("session must be a string")
        row, sa = self._authorize_read(attachment_id, principal, session=session)
        link_name = sa["link_name"] if sa is not None else None
        path = (
            str(self.store.session_dir(sa["session"]) / link_name)
            if sa is not None else None
        )
        return {
            "ok": True,
            "attachment": _format_meta(row, link_name=link_name, path=path),
        }

    def list(self, request, principal=None):
        principal = principal or Principal()
        session = request.get("session")
        include_staged = request.get("include_staged", False)
        if session is not None and not isinstance(session, str):
            raise Refused("session must be a string")
        if not isinstance(include_staged, bool):
            raise Refused("include_staged must be true or false")
        if principal.scope is not None:
            if session is None:
                raise Refused("workflow attachment/list requires a session")
            self._check_session_access(session, principal)
        elif principal.command_session is not None:
            if session is None:
                session = principal.command_session
            self._check_session_access(session, principal)
        elif principal.is_command:
            raise Refused("ZERO command cannot list attachments")
        items = []
        if session is not None:
            rows = self.runtime.store.query(
                "SELECT a.*, sa.link_name, sa.session AS sa_session "
                "FROM session_attachments sa "
                "JOIN attachments a ON a.id = sa.attachment_id "
                "WHERE sa.session = ? ORDER BY sa.created_at, sa.attachment_id",
                session,
            )
            for row in rows:
                path = str(self.store.session_dir(session) / row["link_name"])
                items.append(
                    _format_meta(row, link_name=row["link_name"], path=path)
                )
        if session is None or (include_staged and principal.scope is None
                               and not principal.is_command
                               and principal.command_session is None):
            now = time.time()
            staged = self.runtime.store.query(
                "SELECT * FROM attachments "
                "WHERE state = 'staged' AND owner_key = ? "
                "AND (expires_at IS NULL OR expires_at > ?) "
                "ORDER BY created_at, id",
                principal.owner_key, now,
            )
            for row in staged:
                items.append(_format_meta(row))
        return {"ok": True, "attachments": items}

    def prune(self, request, principal=None):
        principal = principal or Principal()
        if (principal.scope is not None or principal.is_command
                or principal.command_session is not None):
            raise Refused("attachment/prune requires a user client")
        include_trashed = request.get("include_trashed", False)
        if not isinstance(include_trashed, bool):
            raise Refused("include_trashed must be true or false")
        stats = self.store.prune(
            self.runtime.store,
            include_trashed=include_trashed,
            live_sessions_dir=self.runtime.paths.sessions,
        )
        return {"ok": True, **stats}

    def prepare_message(self, message, extra, *, principal=None):
        raw = (extra or {}).get("attachments")
        if raw is None:
            return None
        if not isinstance(raw, list) or not raw:
            raise Refused("attachments must be a nonempty list of attachment ids")
        seen = set()
        ids = []
        for item in raw:
            if not isinstance(item, str) or not item:
                raise Refused("each attachment id must be a nonempty string")
            if item not in seen:
                seen.add(item)
                ids.append(item)
        principal = principal or Principal()
        names = []
        for aid in ids:
            row, _ = self._authorize_read(aid, principal)
            names.append(row["name"])
        fallback_title = (
            names[0] if len(names) == 1
            else f"{names[0]} (+{len(names) - 1} more)"
        )
        links_to_materialize = []

        def claim(db, sid, payload):
            now = time.time()
            links_to_materialize.clear()
            child_dicts = list(payload.get("entries") or ())
            for aid in ids:
                row = db.execute(
                    "SELECT * FROM attachments WHERE id = ?", (aid,),
                ).fetchone()
                if row is None:
                    raise Refused(f"unknown attachment {aid!r}")
                if row["state"] == "staged":
                    if row["owner_key"] != principal.owner_key:
                        raise Refused("staged attachment belongs to another caller")
                    if row["expires_at"] is not None and row["expires_at"] <= now:
                        raise Refused("staged attachment lease has expired")
                    db.execute(
                        "UPDATE attachments SET state = 'claimed', expires_at = NULL "
                        "WHERE id = ?",
                        (aid,),
                    )
                else:
                    existing_any = db.execute(
                        "SELECT session FROM session_attachments "
                        "WHERE attachment_id = ?",
                        (aid,),
                    ).fetchall()
                    if not existing_any:
                        raise Refused("attachment is no longer accessible")
                    if principal.scope is not None:
                        if not any(
                            self.runtime.in_workflow_scope(
                                r["session"], principal.scope,
                            )
                            for r in existing_any
                        ):
                            raise Refused("attachment is outside workflow scope")
                    elif principal.command_session is not None:
                        if not any(
                            r["session"] == principal.command_session
                            for r in existing_any
                        ):
                            raise Refused("attachment does not belong to this session")
                existing_link = db.execute(
                    "SELECT short_id, link_name FROM session_attachments "
                    "WHERE session = ? AND attachment_id = ?",
                    (sid, aid),
                ).fetchone()
                if existing_link is not None:
                    link_name = existing_link["link_name"]
                else:
                    short, link_name = _unique_link_name(db, sid, aid, row["name"])
                    db.execute(
                        "INSERT INTO session_attachments "
                        "(session, attachment_id, short_id, link_name, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (sid, aid, short, link_name, now),
                    )
                link_path = str(self.store.session_dir(sid) / link_name)
                try:
                    meta = json.loads(row["metadata"] or "{}")
                except ValueError:
                    meta = {}
                entry = make_attachment_entry(
                    aid,
                    name=row["name"],
                    media_type=row["media_type"],
                    size_bytes=int(row["size_bytes"]),
                    sha256=row["sha256"],
                    path=link_path,
                    metadata=meta,
                )
                child_dicts.append(entry.to_dict())
                links_to_materialize.append((row["sha256"], link_name))
            payload["attachments"] = ids
            payload["entries"] = child_dicts

        def after_commit(sid):
            self._mark_session_has_attachments(sid)
            self.store.ensure_session_dir(sid)
            for sha256, link_name in links_to_materialize:
                self.store.materialize_session_link(sid, sha256, link_name)

        return PreparedMessage(
            payload={"attachments": ids},
            fallback_title=fallback_title,
            has_content=True,
            claim=claim,
            after_commit=after_commit,
        )

    def session_built(self, sid, session):
        if not valid_session_id(sid):
            return
        folder = self.store.session_dir(sid)
        session.tool_context["attachments_dir"] = folder
        session.tool_context["session_attachments"] = SessionAttachments(
            self, sid,
        )
        # Ensure upgraded or rebuilt sessions have the attachments mount wired.
        for key in ("sandbox", "read_only_sandbox"):
            box = session.tool_context.get(key)
            if box is None:
                continue
            if hasattr(box, "shown") and folder not in box.shown:
                box.shown.append(folder)
            if hasattr(box, "own_attachments"):
                box.own_attachments = folder
        if not self._session_has_attachments(sid):
            return
        rows = self.runtime.store.query(
            "SELECT a.sha256, sa.link_name "
            "FROM session_attachments sa "
            "JOIN attachments a ON a.id = sa.attachment_id "
            "WHERE sa.session = ?",
            sid,
        )
        for row in rows:
            self.store.materialize_session_link(sid, row["sha256"], row["link_name"])

    def _fork_link_map(self, parent_sid, records):
        """Compute deterministic `(attachment_id -> link_name)` for a fork."""
        referenced = list(collect_record_attachments(records))
        seen_ids = {aid for aid, _ in referenced}
        for rec in records:
            if not core_entries.has(rec.entry, "context_boundary"):
                continue
            for item in rec.entry.get("attachments") or ():
                if not isinstance(item, Mapping):
                    continue
                aid = item.get("attachment_id")
                if isinstance(aid, str) and aid and aid not in seen_ids:
                    seen_ids.add(aid)
                    referenced.append((aid, item))
        if not referenced:
            return {}
        parent_rows = self.runtime.store.query(
            "SELECT attachment_id, link_name FROM session_attachments "
            "WHERE session = ?",
            parent_sid,
        )
        parent_links = {r["attachment_id"]: r["link_name"] for r in parent_rows}
        used = set()
        assigned = {}
        for aid, node in referenced:
            candidate = parent_links.get(aid)
            if not candidate:
                raw_path = node.get("path")
                if isinstance(raw_path, str) and raw_path:
                    candidate = Path(raw_path).name
            if not candidate or candidate in used:
                base_short = short_id_of(aid)
                name = node.get("name") or "file"
                for attempt in range(1, 100):
                    short = base_short if attempt == 1 else f"{base_short}{attempt}"
                    trial = safe_link_name(short, name)
                    if trial not in used:
                        candidate = trial
                        break
            used.add(candidate)
            assigned[aid] = candidate
        return assigned

    def rewrite_fork_records(self, parent_sid, child_sid, records):
        """Rewrite copied attachment paths and compaction handoff sections."""
        link_map = self._fork_link_map(parent_sid, records)
        if not link_map:
            return records
        child_dir = self.store.session_dir(child_sid)

        def rewrite_boundary_attachments(entry):
            raw_list = entry.get("attachments")
            if not isinstance(raw_list, (list, tuple)) or not raw_list:
                return None, False
            updated_list = []
            list_changed = False
            for item in raw_list:
                if not isinstance(item, Mapping):
                    updated_list.append(item)
                    continue
                aid = item.get("attachment_id")
                if isinstance(aid, str) and aid in link_map:
                    new_path = str(child_dir / link_map[aid])
                    if item.get("path") != new_path:
                        updated_list.append({**item, "path": new_path})
                        list_changed = True
                        continue
                updated_list.append(dict(item))
            return updated_list, list_changed

        def rewrite_entry(entry):
            changed = False
            fields = entry.fields
            children = entry.entries
            if entry.type == ATTACHMENT_TYPE:
                aid = entry.get("attachment_id")
                if aid in link_map:
                    new_path = str(child_dir / link_map[aid])
                    if entry.get("path") != new_path:
                        fields = {**(fields or {}), "path": new_path}
                        changed = True
            elif core_entries.has(entry, "context_boundary"):
                updated_list, list_changed = rewrite_boundary_attachments(entry)
                if list_changed:
                    fields = {**(fields or {}), "attachments": updated_list}
                    changed = True
                    if isinstance(fields.get("summary"), str):
                        fields["summary"] = _format_boundary_attachments_section(
                            fields["summary"], updated_list,
                        )
                    if children:
                        rewritten_kids = []
                        for child in children:
                            if (
                                core_entries.has(child, "text")
                                and child.origin == "model"
                                and isinstance(child.get("text"), str)
                            ):
                                new_text = _format_boundary_attachments_section(
                                    child.get("text"), updated_list,
                                )
                                if new_text != child.get("text"):
                                    updated_fields = {
                                        **(child.fields or {}),
                                        "text": new_text,
                                    }
                                    child = replace(child, fields=updated_fields)
                            rewritten_kids.append(child)
                        children = tuple(rewritten_kids)
            if children:
                new_children = tuple(rewrite_entry(c) for c in children)
                if any(a is not b for a, b in zip(new_children, children)):
                    children = new_children
                    changed = True
            if not changed:
                return entry
            return replace(entry, fields=fields, entries=children)

        return [
            replace(rec, entry=updated)
            if (updated := rewrite_entry(rec.entry)) is not rec.entry
            else rec
            for rec in records
        ]

    def session_forked(self, db, parent_sid, child_sid, copied_records):
        """Grant the fork ONLY attachments referenced in the copied prefix."""
        now = time.time()
        referenced = collect_record_attachments(copied_records)
        for aid, node in referenced:
            row = db.execute(
                "SELECT * FROM attachments WHERE id = ?", (aid,),
            ).fetchone()
            if row is None:
                sha = node.get("sha256")
                if not isinstance(sha, str) or not sha:
                    continue
                name = node.get("name") or "file"
                media = node.get("media_type") or "application/octet-stream"
                size = int(node.get("size_bytes") or 0)
                meta = {
                    k: node.get(k) for k in METADATA_KEYS if node.get(k) is not None
                }
                meta_json = json.dumps(meta, sort_keys=True)
                db.execute(
                    "INSERT OR IGNORE INTO attachment_blobs "
                    "(sha256, size_bytes, media_type, metadata, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (sha, size, media, meta_json, now),
                )
                db.execute(
                    "INSERT OR IGNORE INTO attachments "
                    "(id, sha256, name, media_type, size_bytes, metadata, state, "
                    "owner_key, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'claimed', 'local', ?, NULL)",
                    (aid, sha, name, media, size, meta_json, now),
                )
                display_name = name
            else:
                display_name = row["name"]
            # Prefer the exact link_name already rewritten into the copied record.
            raw_path = node.get("path")
            preferred = (
                Path(raw_path).name
                if isinstance(raw_path, str) and raw_path else None
            )
            parent_link = db.execute(
                "SELECT short_id, link_name FROM session_attachments "
                "WHERE session = ? AND attachment_id = ?",
                (parent_sid, aid),
            ).fetchone()
            link_name = preferred or (
                parent_link["link_name"] if parent_link is not None else None
            )
            short = (
                parent_link["short_id"]
                if parent_link is not None else short_id_of(aid)
            )
            if link_name:
                taken = db.execute(
                    "SELECT 1 FROM session_attachments "
                    "WHERE session = ? AND link_name = ?",
                    (child_sid, link_name),
                ).fetchone()
                if taken:
                    short, link_name = _unique_link_name(
                        db, child_sid, aid, display_name,
                    )
            else:
                short, link_name = _unique_link_name(db, child_sid, aid, display_name)
            db.execute(
                "INSERT OR IGNORE INTO session_attachments "
                "(session, attachment_id, short_id, link_name, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (child_sid, aid, short, link_name, now),
            )
            self._mark_session_has_attachments(child_sid)

    def after_fork(self, child_sid, copied_records):
        """Materialize hardlinks ONLY for the fork's copied prefix."""
        if not valid_session_id(child_sid):
            return
        if not self._session_has_attachments(child_sid):
            return
        rows = self.runtime.store.query(
            "SELECT a.sha256, sa.link_name "
            "FROM session_attachments sa "
            "JOIN attachments a ON a.id = sa.attachment_id "
            "WHERE sa.session = ?",
            child_sid,
        )
        if rows:
            self._mark_session_has_attachments(child_sid)
            self.store.ensure_session_dir(child_sid)
            for row in rows:
                self.store.materialize_session_link(
                    child_sid, row["sha256"], row["link_name"],
                )

    def before_trash(self, sid):
        """Preserve blob refs for GC before moving the session log to trash."""
        if not self._session_has_attachments(sid):
            return
        now = time.time()

        def change(db):
            rows = db.execute(
                "SELECT sa.attachment_id, a.sha256 "
                "FROM session_attachments sa "
                "JOIN attachments a ON a.id = sa.attachment_id "
                "WHERE sa.session = ?",
                (sid,),
            ).fetchall()
            if rows:
                db.executemany(
                    "INSERT OR IGNORE INTO trashed_attachments "
                    "(trash_id, attachment_id, sha256, trashed_at) "
                    "VALUES (?, ?, ?, ?)",
                    [(sid, row["attachment_id"], row["sha256"], now) for row in rows],
                )

        self.runtime.store.write(change)

    def session_deleted(self, sid):
        self._unmark_session_has_attachments(sid)
        self.store.remove_session_dir(sid)
        self._auto_gc()

    def rebuild(self, db, sid, records):
        """Re-index attachment rows and session grants from the session log."""
        now = time.time()
        referenced = collect_record_attachments(records)
        for aid, node in referenced:
            sha = node.get("sha256")
            if not isinstance(sha, str) or not sha:
                continue
            name = node.get("name") or "file"
            media = node.get("media_type") or "application/octet-stream"
            size = int(node.get("size_bytes") or 0)
            meta = {
                k: node.get(k) for k in METADATA_KEYS if node.get(k) is not None
            }
            meta_json = json.dumps(meta, sort_keys=True)
            db.execute(
                "INSERT OR IGNORE INTO attachment_blobs "
                "(sha256, size_bytes, media_type, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (sha, size, media, meta_json, now),
            )
            db.execute(
                "INSERT OR IGNORE INTO attachments "
                "(id, sha256, name, media_type, size_bytes, metadata, state, "
                "owner_key, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'claimed', 'local', ?, NULL)",
                (aid, sha, name, media, size, meta_json, now),
            )
            db.execute(
                "UPDATE attachments SET state = 'claimed', expires_at = NULL "
                "WHERE id = ? AND state != 'claimed'",
                (aid,),
            )
            existing = db.execute(
                "SELECT 1 FROM session_attachments "
                "WHERE session = ? AND attachment_id = ?",
                (sid, aid),
            ).fetchone()
            if existing is None:
                raw_path = node.get("path")
                candidate = (
                    Path(raw_path).name
                    if isinstance(raw_path, str) and raw_path else None
                )
                if candidate:
                    taken = db.execute(
                        "SELECT 1 FROM session_attachments "
                        "WHERE session = ? AND link_name = ?",
                        (sid, candidate),
                    ).fetchone()
                    if not taken:
                        short = short_id_of(aid)
                        link_name = candidate
                    else:
                        short, link_name = _unique_link_name(db, sid, aid, name)
                else:
                    short, link_name = _unique_link_name(db, sid, aid, name)
                db.execute(
                    "INSERT INTO session_attachments "
                    "(session, attachment_id, short_id, link_name, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (sid, aid, short, link_name, now),
                )
            self._mark_session_has_attachments(sid)

    def message_label(self, entry):
        return attachment_label(entry) or None

    def message_restore(self, entry):
        ids = attachment_ids(entry)
        return {"attachments": ids} if ids else None
