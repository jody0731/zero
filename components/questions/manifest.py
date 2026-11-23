"""Component manifest for questions."""

from core.components import ComponentManifest, SessionBuild
from components.questions.hooks import Questions


def session_hooks(build: SessionBuild):
    return [Questions()]


MANIFEST = ComponentManifest(
    name="questions",
    order=8,
    session_hooks=session_hooks,
)
