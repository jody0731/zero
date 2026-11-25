"""Soft attachment-limit ask hook for `present_file`."""

from pathlib import Path

from components.attachments.store import soft_cap_bytes
from components.files import tool as files_tool
from core.hooks import Hook


def _over_cap_info(call):
    """Return `(display_name, reason)` when `present_file` exceeds soft caps."""
    if call.name != "present_file":
        return None
    if call.args.get("allow_over_limit") is True:
        return None
    raw_path = call.args.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    ctx = call.turn.session.tool_context
    box = ctx.get("sandbox")
    if box is None:
        return None
    try:
        stat_ans = files_tool.run_sandboxed_io(
            box, {"op": "stat", "path": raw_path, "sniff": 0},
        )
    except Exception:
        return None
    if "error" in stat_ans or stat_ans.get("kind") != "file":
        return None
    size_bytes = int(stat_ans.get("size") or 0)
    shown = stat_ans.get("path") or raw_path
    display_name = (
        str(call.args.get("title") or "").strip()
        or Path(shown).name
        or "file"
    )
    limits = ctx.get("limits")
    sess_att = ctx.get("session_attachments")
    if sess_att is not None:
        why = sess_att.over_limit_reason(size_bytes, limits=limits)
        return (display_name, why) if why else None
    max_file, _ = soft_cap_bytes(limits)
    if max_file is not None and size_bytes > max_file:
        limit_mb = max_file / (1024 * 1024)
        why = (
            f"file size ({size_bytes:,} bytes) exceeds soft limit "
            f"({limit_mb:g} MiB)"
        )
        return display_name, why
    return None


class PresentFileLimit(Hook):
    """Marks over-cap `present_file` calls for the mode's approver with fallback."""

    def allows_side_by_side(self, call):
        if call.name != "present_file":
            return True
        return _over_cap_info(call) is None

    def before_tool(self, call):
        hit = _over_cap_info(call)
        if hit is None:
            return None
        display_name, why = hit
        call.notes["limit"] = {
            "rule": "attachment_limit",
            "action": "ask",
            "reason": (
                f"snapshotting {display_name} exceeds the soft attachment "
                f"limit ({why})"
            ),
            "about": (
                f"Snapshotting {display_name} exceeds this session's soft "
                f"attachment limit ({why}). Declining keeps the workspace "
                "path reference without snapshotting."
            ),
        }
        call.notes["soft_limit_fallback"] = True
        return None
