"""Component manifest for limits."""

from core.components import ComponentManifest, SessionBuild
from components.limits import SETTINGS
from components.limits.repeats import RepeatGuard
from components.limits.stops import BrokenReplyLimit


def session_hooks(build: SessionBuild):
    return [
        RepeatGuard(build.limits),
        BrokenReplyLimit(build.limits),
    ]


MANIFEST = ComponentManifest(
    name="limits",
    order=60,
    settings=SETTINGS,
    session_hooks=session_hooks,
)
