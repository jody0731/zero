"""Model component manifest: servers, retries, entries, and ModelService."""

import contextlib
import io

from core.components import ComponentManifest
from core.llm import read_replies_with
from components.model import SETTINGS
from components.model import servers as model_servers
from components.model.entries import ENTRY_SPECS, reply_from_stream
from components.model.retry import ModelRetry
from components.model.servers import Model, ModelServerError, resolve
from components.model.service import ModelService


def register():
    """Install the model component's stream-to-entry reader."""
    read_replies_with(reply_from_stream)


def _prepare_session(build):
    read_replies_with(reply_from_stream)
    client = build.options.get("client")
    if build.resolved is None:
        build.resolved = resolve(build.model, check_endpoint=client is None)
    if build.server is None:
        build.server = build.resolved[0]
    server = build.server
    if "thinking_effort" in build.options:
        thinking_effort = build.options["thinking_effort"]
    else:
        thinking_effort = build.settings.get("thinking_effort")
    fallback_model = build.options.get("fallback_model")
    fallback_client = build.options.get("fallback_client")

    def fallback_name():
        return fallback_model or build.limits["fallback_model"]

    def silence():
        return build.limits["model_silence_seconds"]

    llm = Model(
        build.model,
        client,
        timeout=silence,
        effort=thinking_effort,
        resolved=build.resolved,
    )
    spares = {}

    def fallback():
        name = fallback_name()
        if not name or name == build.model:
            return None
        if name not in spares:
            try:
                spares[name] = Model(
                    name,
                    fallback_client,
                    timeout=silence,
                    effort=thinking_effort,
                )
            except ModelServerError:
                return None
        return spares[name]

    build.state["llm"] = llm
    build.state["fallback_name"] = fallback_name
    build.state["fallback"] = fallback
    build.settings["server"] = server.name
    build.settings["context_limit"] = server.context_window
    build.settings["price"] = server.price
    build.settings["thinking_effort"] = thinking_effort


def _session_hooks(build):
    return [ModelRetry(build.limits)]


def _service(build):
    read_replies_with(reply_from_stream)
    return ModelService()


def _doctor(workspace=None):
    notes = []
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            notes.extend(model_servers.notes())
        except ModelServerError as error:
            notes.append(str(error))
    return [("!", note) for note in notes]


MANIFEST = ComponentManifest(
    name="model",
    order=12,
    entries=ENTRY_SPECS,
    settings=SETTINGS,
    prepare_session=_prepare_session,
    session_hooks=_session_hooks,
    service=_service,
    doctor=_doctor,
)
