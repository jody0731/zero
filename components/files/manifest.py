"""Component manifest for files."""

from core.components import ComponentManifest, ServiceBuild, SessionBuild
from components.files import SETTINGS
from components.files.seen import ChangedFiles, SeenFiles
from components.files.service import FilesService


def session_hooks(build: SessionBuild):
    # On a resume, the files the cut-off turn touched that changed meanwhile.
    return [ChangedFiles()]


def after_session(session, build: SessionBuild):
    # What the file tools saw, from the session's log as it stands.
    session.tool_context["files"] = SeenFiles(lambda: session.log)


def service(build: ServiceBuild):
    return FilesService()


MANIFEST = ComponentManifest(
    name="files",
    order=70,
    settings=SETTINGS,
    session_hooks=session_hooks,
    after_session=after_session,
    service=service,
)
