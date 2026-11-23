"""Subagents component manifest: helper sessions and estimate checks."""

from core.components import ComponentManifest
from components.subagents import SETTINGS
from components.subagents.hooks import PastEstimate
from components.subagents.service import SubagentsService


def _session_hooks(build):
    return [PastEstimate()]


def _service(build):
    return SubagentsService()


MANIFEST = ComponentManifest(
    name="subagents",
    order=85,
    settings=SETTINGS,
    session_hooks=_session_hooks,
    service=_service,
)
