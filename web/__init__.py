"""The web tools: web_search and web_fetch, run in the server, never in the box.

tool.py: the tools, and the `Web` they reach the network with (the search's
client where a DeepSeek key exists, `web_clients`).
"""

from settings import Setting

SETTINGS = (
    # The time a fetch's or a search's every request may take
    Setting("web_timeout", "Limits", "Seconds a web request may take", 30,
            "number"),
)
