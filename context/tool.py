"""`compact`: ZERO's own way to ask for a compaction, between one step and the
next (hooks.py does it before the next model call)."""

from core.tools import tool

DESCRIPTION = (
    "Compact this session's context before your next step: older outputs are "
    "saved to files and cleared from view, and when that isn't enough, the "
    "steps before the newest ones are replaced by a handoff you write. The "
    "session's log keeps everything. It gives room, and every later step "
    "reads a smaller context; the step after it reads the changed context "
    "as new input, not from the cache, and what was cleared is read again "
    "from its file. A compaction also comes on its own when the context "
    "nears its line."
)
DONE = "A compaction comes before your next step."


@tool(
    DESCRIPTION,
    summary=lambda args: "compact this session's context",
    # It changes nothing on the machine
    needs_permission=lambda args: False,
)
def compact(notes=None):
    """`notes`: the call's own, where the hook finds the request."""
    if notes is not None:
        notes["compact"] = True
    return DONE


# What this component offers the model, in the order it reads them; the
# assembly adds them to core's registry (core/tools.py, add)
TOOLS = (compact,)
