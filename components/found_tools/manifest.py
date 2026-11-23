"""Component manifest for found_tools."""

from core import tools
from core.components import ComponentManifest, SessionBuild


def after_session(session, build: SessionBuild):
    # The tools found at run time a session can call but doesn't send.
    session.tool_context["deferred_tools"] = (
        lambda: tools.deferred(session.tool_context)
    )


MANIFEST = ComponentManifest(
    name="found_tools",
    order=90,
    after_session=after_session,
)
