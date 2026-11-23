"""Rounds component manifest: multi-round hand-offs and unattended tracking."""

from core.components import ComponentManifest
from components.rounds import SETTINGS
from components.rounds.hooks import Rounds, running_round
from components.rounds.service import RoundsService


def _prepare_session(build):
    build.state.setdefault("unattended_checks", []).append(
        lambda turn: running_round(turn) is not None
    )


def _session_hooks(build):
    return [Rounds()]


def _service(build):
    return RoundsService()


MANIFEST = ComponentManifest(
    name="rounds",
    order=80,
    settings=SETTINGS,
    prepare_session=_prepare_session,
    session_hooks=_session_hooks,
    service=_service,
)
