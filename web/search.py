"""Search service selection and the destination bound to a lookup."""

import hashlib
import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from model import servers
from model.apis import KEYLESS_SEARCH_APIS, SEARCH_APIS
from model.apis.search import is_http_url


@dataclass(frozen=True)
class SearchService:
    name: str
    title: str
    url: str
    api: object
    model: str | None = None
    key: str | None = field(default=None, repr=False)

    @property
    def label(self):
        if self.title.casefold() == self.name:
            return self.title
        return f"{self.title} ({self.name})"

    def binding(self, query, count):
        """A stable identity for the destination and the search input."""
        fields = self.name, self.url, self.api.NAME, self.model, query, count
        return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


def available_services():
    """Eligible services in preference order. Nothing is probed over the network."""
    found = {}
    try:
        keys = servers.read_keys()
    except servers.ModelServerError:
        keys = {}
    for name, entry in servers.table().items():
        api = SEARCH_APIS.get(entry.get("search_api"))
        if api is None or not is_http_url(entry["url"]):
            continue
        parts = urlsplit(entry["url"])
        if parts.query or parts.fragment:
            continue
        model = entry.get("search_model")
        if getattr(api, "REQUIRES_MODEL", False) and not model:
            continue
        server = servers.ModelServer(name, entry)
        key = servers.environment_key(server) or servers.saved_key(name, keys)
        if not key or key == servers.NO_KEY:
            continue
        found[name] = SearchService(name, server.title, api.endpoint(server.url),
                                    api, model, key)
    for api in KEYLESS_SEARCH_APIS:
        found.setdefault(api.NAME, SearchService(api.NAME, api.TITLE, api.URL, api))
    return found


def select(services, model_server, saved, requested=None):
    """The chosen service and the reason, including an unavailable saved choice."""
    named = requested if requested is not None else saved
    if named is not None:
        service = services.get(named)
        if service is None:
            return None, f"Search service {named!r} is unavailable."
        reason = "selected in settings" if named == saved else "named for this lookup"
        return service, reason
    if model_server in services and services[model_server].key:
        return services[model_server], "Automatic chose the session's model server"
    for service in services.values():
        if service.key:
            return service, "Automatic chose another model server with a search key"
    if services:
        return next(iter(services.values())), "Automatic chose keyless search"
    return None, "No search service is available."
