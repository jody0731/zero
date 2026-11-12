"""`usage`: what this session has used, ZERO's to read whenever it weighs a
means (principle 3: its own estimate is the yardstick, and no limit stops
it).

The session's use comes from its log (spend.py); its subagents' from the
server that hands it `session_use.subagents` (subagents/service.py), none
without one.
"""

from core.tools import tool
from usage.spend import against_estimate, describe, tokens_text

NOT_HERE = "Usage isn't available in this session."
# The context's size as the last call's server counted it, and what a step
# counts for reading it again: the numbers of the context's own note
# (context/size_note.py), here to look at again
CONTEXT = "The context now: {tokens:,} tokens; a step reads it again for {step}."


def subagent_line(entry):
    """One subagent as `usage` lists it."""
    line = (f"- {entry['id']} \"{entry['title']}\" ({entry['state']}): "
            f"{describe(entry['use'])}")
    if entry.get("estimate"):
        line += ", " + against_estimate(entry["use"], *entry["estimate"])
    return line


@tool(
    "What this session has used so far: this turn's and the whole session's "
    "working time, the tokens of its model calls (in, the share from the "
    "cache, out) and their total in weighted tokens. The session's counts "
    "every subagent under it; "
    "each subagent this session started is listed with its use, its own "
    "subagents' counted in, against the estimate it was started with. Then "
    "the context's size now, as the last call's server counted it, and what "
    "a step counts for reading it again.",
    summary=lambda args: "usage: this session's",
    needs_permission=lambda args: False,
    changing=True,
)
def usage(session_use=None):
    """`session_use`: the session's (spend.SessionUse), from the assembly."""
    if session_use is None:
        return NOT_HERE
    own = session_use.own()
    lines = [f"This turn: {describe(session_use.turn())}."]
    found = session_use.subagents() if session_use.subagents else []
    total = own
    for entry in found:
        total = total + entry["use"]
    if found:
        lines.append(
            f"This session in all, its subagents' included: {describe(total)} "
            f"(its own: {describe(own)})."
        )
    else:
        lines.append(f"This session in all: {describe(own)}.")
    context = session_use.context()
    if context is not None:
        tokens, step = context
        lines.append(CONTEXT.format(tokens=tokens, step=tokens_text(step)))
    if found:
        lines.append("Its subagents:")
        lines += [subagent_line(entry) for entry in found]
    return "\n".join(lines)


# What this component offers the model, in the order it reads them; the
# assembly adds them to core's registry (core/tools.py, add)
TOOLS = (usage,)
