"""A step's model call waits for the MCP servers still starting, a little: a
turn that comes as ZERO's server starts (`zero -p`, or the first message
after an idle stop) sees the tools its user listed, not a model that finds
none (Pi waits up to 10 s at its first prompt). The wait is the turn's own,
for its own tools, a stop at hand; a server slower than it is offered at the
step after it is ready.
"""

from core.hooks import Hook

# The longest a model call waits for servers still starting, in seconds
# (Pi's)
WAIT_SECONDS = 10


class McpServersStarting(Hook):
    """Before each model call: the session's door (the service's, in its
    tool context; none in the evals), waited on while a server starts."""

    def before_model(self, step):
        door = step.turn.session.tool_context.get("mcp_servers")
        if door is not None:
            control = step.turn.control
            door.wait_started(WAIT_SECONDS,
                              None if control is None else control.stop)
        return None
