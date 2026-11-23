"""Component manifest for session_search."""

from core.components import ComponentManifest, ServiceBuild
from components.session_search import SETTINGS
from components.session_search.service import SessionSearchService


def service(build: ServiceBuild):
    return SessionSearchService()


MANIFEST = ComponentManifest(
    name="session_search",
    order=85,
    settings=SETTINGS,
    service=service,
)
