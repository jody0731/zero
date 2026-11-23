"""Component manifest for web."""

import settings
from core.components import ComponentManifest, SessionBuild
from components.model import servers as model_servers
from components.model.servers import ModelServerError
from components.web import SETTINGS
from components.web import tool as web_tool
from components.web.search import available_services, select as select_search_service


def prepare_session(build: SessionBuild):
    web = build.options.get("web")
    build.tool_context["web"] = (
        web if web is not None else web_tool.Web(model_server=build.server.name)
    )


def session_hooks(build: SessionBuild):
    return [web_tool.SearchApproval(build.ask)]


def search_service_status():
    """The default session's search selection, without contacting a service."""
    try:
        config = settings.load()
        model = config["model"]
        server = (
            model_servers.model_address(model, model_servers.table())[0]
            if model
            else None
        )
        available = available_services()
        service, reason = select_search_service(
            available, server, config["web_search_service"],
        )
    except (settings.SettingsError, ModelServerError):
        return False, "Web search service could not read its configuration."
    choices = ", ".join(available)
    if service is None:
        return False, f"Web search. {reason} Available services are {choices}."
    return True, (
        f"Web search uses {service.label}. {reason}. "
        f"Available services are {choices}."
    )


def doctor(workspace=None):
    available, status = search_service_status()
    return [("✓" if available else "!", status)]


MANIFEST = ComponentManifest(
    name="web",
    order=75,
    settings=SETTINGS,
    prepare_session=prepare_session,
    session_hooks=session_hooks,
    doctor=doctor,
)
