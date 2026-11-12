"""The MCP servers' service in ZERO's server (server/services.py): the door
made and started with the server (its servers in the background) and closed
at its stop; the door in each session the server builds; and the messages
`mcp/list`, `mcp/get`, `mcp/add` and
`mcp/remove`, which the `zero mcp` commands send. A server listed by
`add` is saved first, then started, and its reply says how its start went:
one that fails stays listed, to be fixed and tried at the next start. The
evals' server has no list (`listed_file` None): no door, no tools.
"""

import dataclasses

from server.services import Refused, Service
from mcp_servers import listed
from mcp_servers.connections import McpServers

NO_LIST = "this server reads no MCP servers' list (an eval's server)"


class McpServersService(Service):
    name = "mcp_servers"
    looking_messages = ("mcp/list", "mcp/get")

    def __init__(self, listed_file, limits):
        """`listed_file()`: the user's list (paths.mcp_servers_file), None for
        none; `limits`: the settings as they stand."""
        self.listed_file = listed_file
        self.door = None if listed_file is None else McpServers(
            listed_file, limits)

    def begin(self):
        if self.door is not None:
            self.door.start()

    def shutdown(self):
        if self.door is not None:
            self.door.stop()

    def session_built(self, sid, session):
        """The door, through which the session's tools reach the servers."""
        if self.door is not None:
            session.tool_context["mcp_servers"] = self.door

    def messages(self):
        return {"mcp/list": self.list_message,
                "mcp/get": self.get_message,
                "mcp/add": self.add_message,
                "mcp/remove": self.remove_message}

    def the_door(self):
        if self.door is None:
            raise Refused(NO_LIST)
        return self.door

    def list_message(self, request):
        door = self.the_door()
        # What the file says now, beside what runs
        return {"ok": True, "servers": door.rows(),
                "notes": listed.read(self.listed_file()).notes}

    def get_message(self, request):
        name = request.get("name")
        if not isinstance(name, str):
            raise Refused("mcp/get needs a name string")
        found = self.the_door().details(name)
        if found is None:
            raise Refused(f"no MCP server named {name} is listed")
        return {"ok": True, "server": found}

    def add_message(self, request):
        door = self.the_door()
        name = request.get("name")
        if not isinstance(name, str) or not listed.NAME.match(name):
            raise Refused(f"an MCP server's name is {listed.NAME_RULE}")
        notes = []
        agent = request.get("from")
        if agent is not None:
            try:
                entry, notes = listed.from_agent(str(agent), name)
            except LookupError as error:
                raise Refused(str(error)) from None
            narrowed = request.get("tools")
            if narrowed is not None:
                if listed.checked(narrowed, listed.STRINGS) is None:
                    raise Refused("tools is a list of strings")
                entry = dataclasses.replace(entry, tools=tuple(narrowed))
        else:
            table = {key: request[key] for key in listed.FIELDS
                     if request.get(key) is not None}
            entry, said = listed.entry_of(name, table)
            if entry is None:
                raise Refused("; ".join(said))
            notes += said
        try:
            listed.add(entry, self.listed_file())
        except ValueError as error:
            raise Refused(str(error)) from None
        door.start_server(entry)
        door.wait_for(name, (entry.start_seconds
                             or door.limits["mcp_start_seconds"]) + 1)
        return {"ok": True, "server": door.details(name), "notes": notes}

    def remove_message(self, request):
        name = request.get("name")
        if not isinstance(name, str):
            raise Refused("mcp/remove needs a name string")
        door = self.the_door()
        try:
            listed.remove(name, self.listed_file())
        except LookupError as error:
            raise Refused(str(error)) from None
        door.stop_server(name)
        return {"ok": True, "removed": name}
