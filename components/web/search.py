"""Search service discovery, Same-Provider Default selection, and lookup binding."""

import hashlib
import json
from dataclasses import dataclass, field

from components.model import SERVICE_CURRENT_SERVER, SERVICE_OFF, servers
from components.model.apis import discover_utility_services, select_utility_service


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
    discovered = servers.utility_services("search", discover_utility_services)
    return {
        name: SearchService(
            item.name, item.title, item.url, item.api, item.model, item.key,
        )
        for name, item in discovered.items()
    }


def eligible_services(services, model_server, saved):
    """Subset of `services` authorized by `saved` under Same-Provider Default."""
    setting = SERVICE_CURRENT_SERVER if saved is None else saved
    if setting == SERVICE_OFF:
        return {}
    if setting != SERVICE_CURRENT_SERVER:
        service = services.get(setting)
        return {setting: service} if service is not None else {}
    return {
        name: service for name, service in services.items()
        if not service.key or name == model_server
    }


def select(services, model_server, saved, requested=None):
    """The chosen service and the reason under Same-Provider Default."""
    return select_utility_service(
        services, model_server, saved, requested, label="Search service",
    )
