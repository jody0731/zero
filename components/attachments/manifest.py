"""Attachments component manifest: settings, entries, sandbox mount and service."""

from pathlib import Path

from components.attachments import SETTINGS
from components.attachments.entries import ENTRY_SPECS
from components.attachments.hooks import HostedMediaHook
from components.attachments.service import AttachmentsService
from components.attachments.store import valid_session_id
from core.components import ComponentManifest, ServiceBuild, SessionBuild


def prepare_session(build: SessionBuild):
    """Wire a real server session's attachments folder for lazy RO bind-mounting."""
    if not build.options.get("on_a_server"):
        return
    # Skip temporary logs created solely to obtain an upgrade header.
    if type(build.log).__name__ == "_FreshLog":
        return
    sid = build.log.path.stem
    if not valid_session_id(sid):
        return
    state_dir = build.log.path.parent.parent
    attachments_dir = state_dir / "attachments" / "sessions" / sid
    build.tool_context["attachments_dir"] = attachments_dir
    if (add_shown := build.state.get("add_sandbox_shown")) is not None:
        add_shown(attachments_dir)
    else:
        build.state.setdefault("sandbox_shown", []).append(attachments_dir)
    for key in ("sandbox", "read_only_sandbox"):
        box = build.state.get(key)
        if box is not None and hasattr(box, "own_attachments"):
            box.own_attachments = attachments_dir


def service(build: ServiceBuild):
    lease = build.options.get("attachment_lease_seconds")
    if lease is not None:
        return AttachmentsService(lease_seconds=lease)
    return AttachmentsService()


def session_hooks(build: SessionBuild):
    return [HostedMediaHook()]


MANIFEST = ComponentManifest(
    name="attachments",
    order=35,
    entries=ENTRY_SPECS,
    settings=SETTINGS,
    prepare_session=prepare_session,
    session_hooks=session_hooks,
    service=service,
)
