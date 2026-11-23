"""Component manifest for usage."""

from core.components import ComponentManifest, SessionBuild
from components.usage.spend import SessionUse, token_weights


def prepare_session(build: SessionBuild):
    build.state["token_weights"] = token_weights(build.server.price)


def after_session(session, build: SessionBuild):
    # What the session has used, for the usage tool (a server adds its subagents').
    session.tool_context["session_use"] = SessionUse(lambda: session)


MANIFEST = ComponentManifest(
    name="usage",
    order=15,
    prepare_session=prepare_session,
    after_session=after_session,
)
