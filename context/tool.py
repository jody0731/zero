"""`compact`: ZERO's own way to ask for a compaction, between one step and the
next (hooks.py does it before the next model call)."""

from core.tools import tool

DESCRIPTION = (
    "Compact this session's context before your next step: older outputs are "
    "saved to files and cleared from view, and everything before the step "
    "that calls it is replaced by a handoff you write. The session's log "
    "keeps everything. It gives room, and every later step "
    "reads a smaller context; the step after it reads the changed context "
    "as new input, not from the cache, and what was cleared is read again "
    "from its file. A compaction also comes on its own when the context "
    "nears its line."
)
# The words ZERO may give: what the handoff keeps, beside its fixed parts
# (compaction.py's INSTRUCTIONS)
INSTRUCTIONS = (
    "What the handoff keeps, in your words, beside its usual parts (the "
    "user's requests, their constraints, what was done, the state, the next "
    "step): what the work ahead needs from the steps it replaces."
)
DONE = "A compaction comes before your next step."


def summary(args):
    """The call in a line: what it keeps, where ZERO said."""
    words = (args.get("instructions") or "").strip()
    if not words:
        return "compact this session's context"
    return f"compact this session's context, keeping: {words}"


@tool(
    DESCRIPTION,
    optional=("instructions",),
    summary=summary,
    # It changes nothing on the machine
    needs_permission=lambda args: False,
    instructions=("string", INSTRUCTIONS),
)
def compact(instructions=None, notes=None):
    """`notes`: the call's own, where the hook finds the request and its
    words."""
    if notes is not None:
        notes["compact"] = True
        if (instructions or "").strip():
            notes["compact_instructions"] = instructions.strip()
    return DONE


# What this component offers the model, in the order it reads them; the
# assembly adds them to core's registry (core/tools.py, add)
TOOLS = (compact,)
