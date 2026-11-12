"""MCP servers: the tools of others' MCP servers, as ZERO's model's tools.

ZERO is their MCP client (`zero mcp serve`, apps/mcp/, is the other side:
ZERO as an MCP server for other agents). The user lists the servers in
ZERO's home (listed.py: mcp_servers.toml, `zero mcp add|list|get|remove`,
an entry copied from another agent's list); the server starts each in the
background when it starts and stops them when it stops (service.py); their
tools reach a session through the door the service hands it, never a list
filled at import (connections.py, McpServers); the adapter (adapter.py)
translates between MCP and ZERO and states the protocol versions it speaks.
A tool's text and its answers reach the model marked as its server's, and
what a server says of its own tools' permissions isn't read: every call
needs permission, asked as a call that leaves the sandbox is (hooks.py, the
first step's wait for servers still starting).
"""

from settings import Setting

SETTINGS = (
    # Codex's, OpenCode's and OpenClaw's 30 s to start
    Setting("mcp_start_seconds", "Limits",
            "Seconds an MCP server may take to start", 30, "number"),
    # DeepSeek Harness's, OpenClaw's and Pi's 60 s a call; an entry's
    # call_seconds wins for its server
    Setting("mcp_call_seconds", "Limits",
            "Seconds an MCP server's tool may take to answer", 60, "number"),
)
