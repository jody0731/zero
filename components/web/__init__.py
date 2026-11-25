"""Search and fetch run through the server's network connection."""

from components.model import SERVICE_CHOICES, SERVICE_CURRENT_SERVER
from settings import Setting

SETTINGS = (
    Setting("search_service", "Default", "Web search service",
            SERVICE_CURRENT_SERVER, "service", choices=SERVICE_CHOICES,
            aliases=("web_search_service",), switchable=True,
            none_means_default=True),
    # The time a fetch's or a search's every request may take
    Setting("web_timeout", "Limits", "Seconds a web request may take", 30,
            "number"),
    Setting("web_fetch_max_megabytes", "Limits", "Megabytes a web fetch reads",
            5, "int"),
    Setting("web_fetch_max_redirects", "Limits", "Redirects a web fetch follows",
            5, "int", minimum=0),
    Setting("web_search_max_results", "Limits",
            "Results a web search may ask for (1 to 50)", 50, "int",
            maximum=50),
)
