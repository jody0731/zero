"""The APIs ZERO speaks to model servers, a file each. A server can pin
its API in models.toml. Otherwise ZERO prefers Responses and uses Chat
Completions where the endpoint or model requires it.

Each API adapter defines `NAME`, `TITLE`, `FEATURES` (what ZERO may ask of a
model through the API's standard fields; what a model honours of them is
measured, features.py), `MIN_OUTPUT_TOKENS` (the fewest output tokens a
request may ask for), `request_options(server, *, encrypted=False,
zero_temperature=False)`, `keeps_continuation(server)`, `open_stream(client,
model, conversation, **request)` (one streamed request from ZERO's entries
and the request's standard fields: tools, effort, json_schema, json_mode,
temperature, max_output_tokens), `fold(draft, event)` (one event into the
reply's draft, core/llm.py), and optional `alive(event)`. ZERO sends each
API's standard fields as they are and reads replies by its contract, whatever
the server: what a server doesn't honour is the server's, measured and said,
never patched.
"""

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from components.model import (
    ModalitySupport,
    ModelCapabilities,
    SERVICE_CURRENT_SERVER,
    SERVICE_OFF,
)
from components.model.apis import (
    chat_completions,
    deepseek_search,
    google_interactions,
    images,
    moonshot_search,
    responses,
    stt,
    tavily_search,
    tts,
    videos,
)
from components.model.apis.search import is_http_url

APIS = {
    api.NAME: api
    for api in (chat_completions, responses, google_interactions)
}
DEFAULT = "responses"

# Adapter-declared capabilities per wire API, intersected with resolved model
# capabilities to compute effective support per (model, endpoint, api, role).
ADAPTER_CAPABILITIES = {
    "responses": ModelCapabilities(
        input=ModalitySupport(
            text=True, image=True, audio=False, video=False, pdf=True,
        ),
        output=ModalitySupport(
            text=True, image=False, audio=False, video=False, pdf=False,
        ),
        reasoning=True,
        tool_calling=True,
        api="responses",
    ),
    "chat_completions": ModelCapabilities(
        input=ModalitySupport(
            text=True, image=True, audio=True, video=False, pdf=True,
        ),
        output=ModalitySupport(
            text=True, image=False, audio=False, video=False, pdf=False,
        ),
        reasoning=True,
        tool_calling=True,
        api="chat_completions",
    ),
    "google_interactions": ModelCapabilities(
        input=ModalitySupport(
            text=True, image=True, audio=True, video=True, pdf=True,
        ),
        output=ModalitySupport(
            text=True, image=False, audio=False, video=False, pdf=False,
        ),
        reasoning=True,
        tool_calling=True,
        api="google_interactions",
    ),
}


def adapter_capabilities(api_or_name, role="chat"):
    """What an API wire adapter itself supports for `role`."""
    name = (
        api_or_name if isinstance(api_or_name, str)
        else getattr(api_or_name, "NAME", None)
    )
    declared = getattr(api_or_name, "ADAPTER_CAPABILITIES", None)
    if declared is None:
        declared = ADAPTER_CAPABILITIES.get(name)
    if declared is None:
        return ModelCapabilities(
            input=ModalitySupport(text=True),
            output=ModalitySupport(text=True),
            reasoning=True,
            tool_calling=(role == "chat"),
            role=role,
            api=name,
        )
    tool_calling = declared.tool_calling if role == "chat" else False
    return ModelCapabilities(
        input=declared.input,
        output=declared.output,
        reasoning=declared.reasoning,
        tool_calling=tool_calling,
        role=role,
        api=name,
    )


# Utility capability registries (media routes register in later waves).
SEARCH_APIS = {
    api.NAME: api
    for api in (
        deepseek_search,
        responses.ResponsesSearch,
        moonshot_search,
        google_interactions.GoogleSearch,
    )
}
KEYLESS_SEARCH_APIS = (tavily_search,)
STT_APIS = {
    api.NAME: api
    for api in (stt.OpenAIAudio, stt.GoogleAudio, stt.DeepSeekAudio)
}
KEYLESS_STT_APIS = (stt.LocalWhisper,)
TTS_APIS = {
    api.NAME: api
    for api in (
        tts.OpenAISpeech,
        tts.OpenAISpeechCompat,
        tts.GoogleSpeech,
        tts.DeepSeekSpeech,
    )
}
KEYLESS_TTS_APIS = ()
IMAGE_APIS = {
    api.NAME: api
    for api in (images.OpenAIImages, images.GoogleImages)
}
KEYLESS_IMAGE_APIS = ()
VIDEO_APIS = {
    api.NAME: api
    for api in (videos.GoogleVideos,)
}
KEYLESS_VIDEO_APIS = ()

UTILITY_CAPABILITIES = {
    "search": ("search_api", "search_model", SEARCH_APIS, KEYLESS_SEARCH_APIS),
    "stt": ("stt_api", "stt_model", STT_APIS, KEYLESS_STT_APIS),
    "tts": ("tts_api", "tts_model", TTS_APIS, KEYLESS_TTS_APIS),
    "image": ("image_api", "image_model", IMAGE_APIS, KEYLESS_IMAGE_APIS),
    "video": ("video_api", "video_model", VIDEO_APIS, KEYLESS_VIDEO_APIS),
}


@dataclass(frozen=True)
class UtilityService:
    """One discovered utility service (search, stt, tts, image, video)."""

    name: str
    title: str
    url: str
    api: object
    model: str | None = None
    key: str | None = field(default=None, repr=False)
    keyless: bool = False
    live_model: str | None = None

    @property
    def label(self):
        if self.title.casefold() == self.name:
            return self.title
        return f"{self.title} ({self.name})"


def discover_utility_services(capability, table, key_for_server):
    """Discover eligible utility services for `capability` with zero probes.

    `table`: configured server mapping ({name: entry}).
    `key_for_server`: callable `(name, entry) -> key str | None`.
    Returns `{service_name: UtilityService}` with keyed servers followed by
    keyless/local fallbacks.
    """
    api_field, model_field, registry, keyless_apis = UTILITY_CAPABILITIES[
        capability
    ]
    found = {}
    for name, entry in table.items():
        api = registry.get(entry.get(api_field))
        if api is None or not is_http_url(entry.get("url")):
            continue
        parts = urlsplit(entry["url"])
        if parts.query or parts.fragment:
            continue
        model = entry.get(model_field)
        if getattr(api, "REQUIRES_MODEL", False) and not model:
            continue
        key = key_for_server(name, entry)
        if not key:
            continue
        title = entry.get("title", name)
        live_model = entry.get("stt_live_model") if capability == "stt" else None
        found[name] = UtilityService(
            name, title, api.endpoint(entry["url"]), api, model, key,
            keyless=False, live_model=live_model,
        )
    for api in keyless_apis:
        avail = getattr(api, "available", None)
        if callable(avail) and not avail():
            continue
        url = getattr(api, "URL", "")
        found.setdefault(
            api.NAME,
            UtilityService(api.NAME, api.TITLE, url, api, keyless=True),
        )
    return found


def select_utility_service(
    services, model_server, saved, requested=None, *, label="Service",
):
    """Resolve one utility service under Same-Provider Default.

    Under "current_server" (including legacy None), only the session's active
    model server and keyless/local fallbacks are eligible; another paid server
    is never billed silently. An explicit pin or "off" is authoritative.
    """
    setting = SERVICE_CURRENT_SERVER if saved is None else saved
    if setting == SERVICE_OFF:
        return None, f"{label} is off."
    if setting != SERVICE_CURRENT_SERVER:
        if requested is not None and requested != setting:
            return None, (
                f"{label} is set to {setting!r}; change the session setting "
                f"to use {requested!r}."
            )
        service = services.get(setting)
        if service is None:
            return None, f"{label} {setting!r} is unavailable."
        return service, "selected in settings"
    if requested is not None:
        service = services.get(requested)
        if service is None:
            return None, f"{label} {requested!r} is unavailable."
        if service.key and service.name != model_server:
            return None, (
                f"{label} {requested!r} is another paid server; under "
                f"{SERVICE_CURRENT_SERVER!r} only the active model server or "
                f"keyless fallbacks may run. Set the session setting to "
                f"{requested!r} to use it."
            )
        return service, "named for this lookup"
    if model_server in services and services[model_server].key:
        return (
            services[model_server],
            "Current server chose the session's model server",
        )
    for service in services.values():
        if not service.key:
            return service, "Current server chose keyless fallback"
    return None, f"No {label.lower()} is available for the current server."
