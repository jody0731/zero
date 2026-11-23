"""Search and fetch run through the server's network connection."""

from settings import Setting

SETTINGS = (
    Setting("web_search_service", "Default", "Web search service", None, "string",
            unset_means="Automatic"),
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
