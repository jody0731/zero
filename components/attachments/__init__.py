"""Attachments: content-addressed file storage, metadata and per-session mounts."""

from settings import Setting

SETTINGS = (
    # 0 means unlimited for both soft caps.
    Setting(
        "max_attachment_megabytes", "Limits",
        "Soft megabyte limit per attachment (0 for unlimited)",
        100, "int", minimum=0,
    ),
    Setting(
        "max_attachment_store_megabytes", "Limits",
        "Soft megabyte limit for stored attachments (0 for unlimited)",
        4096, "int", minimum=0,
    ),
)
