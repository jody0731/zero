"""Component manifest for files."""

import shutil

from core.components import ComponentManifest, ServiceBuild, SessionBuild
from components.files import SETTINGS
from components.files.hooks import PresentFileLimit
from components.files.seen import ChangedFiles, SeenFiles
from components.files.service import FilesService

# Opportunistic system binaries for extended file inspection/rendering.
OPTIONAL_BINARIES = (
    ("soffice", "visual Office (.docx/.pptx/.xlsx) rendering"),
    ("tesseract", "local OCR on scanned PDFs and images"),
    ("ffprobe", "detailed audio/video stream inspection"),
)


def session_hooks(build: SessionBuild):
    # Resume file-change check + soft attachment-cap ask for present_file.
    return [ChangedFiles(), PresentFileLimit()]


def after_session(session, build: SessionBuild):
    # What the file tools saw, from the session's log as it stands.
    session.tool_context["files"] = SeenFiles(lambda: session.log)
    # Live model accessor so vision-capability checks track model switches.
    session.tool_context["files_model"] = lambda: session.model


def service(build: ServiceBuild):
    return FilesService()


def doctor(workspace=None):
    """Report opportunistic file-reader binaries as informational notes."""
    lines = []
    for binary, purpose in OPTIONAL_BINARIES:
        found = shutil.which(binary)
        if found:
            lines.append(("✓", f"Optional {binary} ({purpose}): {found}"))
        else:
            lines.append((
                "!",
                f"Optional {binary} not found ({purpose} unavailable; "
                "core file readers still work)",
            ))
    return lines


MANIFEST = ComponentManifest(
    name="files",
    order=70,
    settings=SETTINGS,
    session_hooks=session_hooks,
    after_session=after_session,
    service=service,
    doctor=doctor,
)
