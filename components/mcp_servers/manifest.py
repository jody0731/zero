"""MCP servers component manifest: external tool connections and doctor checks."""

import shutil

import paths
from core.components import ComponentManifest
from components.mcp_servers import SETTINGS, listed
from components.mcp_servers.hooks import McpServersStarting
from components.mcp_servers.service import McpServersService


def mcp_server_notes():
    """What the user's list of MCP servers says that ZERO can't use as it
    is, and each listed command not found on this machine, a line each."""
    found = listed.read(paths.mcp_servers_file())
    notes = list(found.notes)
    for entry in found.entries:
        if entry.command and shutil.which(entry.command) is None:
            notes.append(
                f"MCP server {entry.name}: its command, "
                f"{entry.command}, isn't found on this machine"
            )
    return notes


def _session_hooks(build):
    return [McpServersStarting()]


def _service(build):
    mcp_servers = build.options.get("mcp_servers", paths.mcp_servers_file)
    return McpServersService(mcp_servers, build.limits)


def _doctor(workspace=None):
    return [("!", note) for note in mcp_server_notes()]


MANIFEST = ComponentManifest(
    name="mcp_servers",
    order=18,
    settings=SETTINGS,
    session_hooks=_session_hooks,
    service=_service,
    doctor=_doctor,
)
