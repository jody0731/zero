"""Component manifest for web."""

import settings
from core.components import ComponentManifest, SessionBuild
from components.model import SERVICE_CURRENT_SERVER, SERVICE_OFF
from components.model import servers as model_servers
from components.model.servers import ModelServerError
from components.web import SETTINGS
from components.web import tool as web_tool
from components.web.search import available_services, select as select_search_service


def prepare_session(build: SessionBuild):
    web = build.options.get("web")
    build.tool_context["web"] = web if web is not None else web_tool.Web()
    if "search_service" in build.options:
        chosen = build.options["search_service"]
    elif "search_service" in build.settings:
        chosen = build.settings["search_service"]
    else:
        chosen = build.limits["search_service"]
    if chosen is None:
        chosen = SERVICE_CURRENT_SERVER
    build.settings["search_service"] = chosen


def after_session(session, build: SessionBuild):
    build.tool_context["web_session"] = session
    web = session.tool_context.get("web")
    if web is not None:
        web.bind_session(session)


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
        chosen_setting = config["search_service"]
        service, reason = select_search_service(available, server, chosen_setting)
    except (settings.SettingsError, ModelServerError):
        return False, "Web search service could not read its configuration."
    choices = ", ".join(available)
    if chosen_setting == SERVICE_OFF:
        return True, f"Web search is off. Available services are {choices}."
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
    after_session=after_session,
    doctor=doctor,
)
