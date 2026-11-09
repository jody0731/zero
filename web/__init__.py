"""The web tools: web_search and web_fetch, run in the server, never in the box.

tool.py: the tools, and the `Web` they reach the network with (the search's
client where a DeepSeek key exists, `web_clients`).
"""

from settings import Setting

SETTINGS = (
    # The time a fetch's or a search's every request may take
    Setting("web_timeout", "Limits", "Seconds a web request may take", 30,
            "number"),
    Setting("web_fetch_max_megabytes", "Limits", "Megabytes a web fetch reads",
            5, "int"),
    Setting("web_fetch_max_redirects", "Limits", "Redirects a web fetch follows",
            5, "int", minimum=0),
    # DeepSeek's Search API takes a count from 1 to 50
    Setting("web_search_max_results", "Limits",
            "Results a web search may ask for (1 to 50)", 50, "int",
            maximum=50),
)
