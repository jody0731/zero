"""The web tools: web_search and web_fetch, run in the server, never in the box.

tool.py: the tools, and the `Web` they reach the network with (the search's
client where a DeepSeek key exists, `web_clients`).
"""

from settings import Setting

SETTINGS = (
    # What the model reads of a page (the whole page is saved to a file); the
    # results a search returns, at most 50 (DeepSeek's own default is 10); the
    # time a fetch's or a search's every request may take
    Setting("web_fetch_chars", "Limits", "Characters a web fetch returns at most",
            20_000, "int", minimum=1000),
    Setting("web_search_results", "Limits", "Results a web search returns", 10, "int"),
    Setting("web_timeout", "Limits", "Seconds a web request may take", 30,
            "number"),
)
