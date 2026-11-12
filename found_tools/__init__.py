"""Found tools: the tools a session can call but doesn't send with its
requests, for room (an MCP server's whose tools don't fit their share of
the line), offered through `tool_search`, which finds them by what they do,
and `call_tool`, which calls one by its name as that tool's own call
(tool.py; the search, index.py). The tools sent stay the same whatever is
found, so a search costs the model's server no cache. Every tool ZERO ships
with is always sent.
"""
