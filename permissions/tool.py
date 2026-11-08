"""`propose_plan`: a plan and the calls it will make, put to the session's
approver as one ask.

The agent decides when a task calls for a plan. The approver
answers it as any ask (asks.py marks it): the user in default, the
judge in auto and auto+. Approved, each listed call is green when it is made
exactly as listed, until the user's next message (grants.py reads
the approved plan from the log); any other call is reviewed as usual.
"""

import json

from core import tools
from core.tools import tool

NAME = "propose_plan"
CALL = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "description": "The tool's name"},
        "arguments": {
            "type": "object",
            "description": "Its arguments, as the call will give them",
        },
    },
    "required": ["tool", "arguments"],
}
APPROVED = (
    "Approved. These calls run without further review when made exactly as "
    "listed, until the user's next message:\n{calls}"
)


def listed(calls, whole=False):
    """The calls, numbered, one summary each (core/tools.py's summary)."""
    lines = []
    for n, call in enumerate(calls, 1):
        arguments = json.dumps(call["arguments"], ensure_ascii=False)
        text = tools.summary(call["tool"], call["arguments"], arguments, whole)
        lines.append(f"{n}. {call['tool']}: {text}")
    return "\n".join(lines)


def plan_summary(args, whole=False):
    """The plan as an ask shows it and the judge reads it (`whole`)."""
    plan = str(args.get("plan", "")).strip()
    return f"plan: {plan}\ncalls:\n{listed(args.get('calls') or [], whole)}"


def problems(args):
    """What is wrong with a plan's calls, before anyone is asked."""
    calls = args.get("calls")
    if not isinstance(calls, list) or not calls:
        return "calls must list the calls the plan will make"
    if not str(args.get("plan", "")).strip():
        return "the plan is empty"
    for n, call in enumerate(calls, 1):
        if not isinstance(call, dict) or not isinstance(call.get("tool"), str):
            return f"call {n} needs a tool's name"
        if not isinstance(call.get("arguments"), dict):
            return f"call {n}'s arguments must be an object"
        if call["tool"] == NAME:
            return f"call {n} is a plan; a plan lists other tools' calls"
        _, error = tools.check(call["tool"], json.dumps(call["arguments"]))
        if error is not None:
            return f"call {n}: {error.removeprefix('Error: ')}"
    return None


@tool(
    "Put a plan to this session's approver with the tool calls it will make, "
    "and wait for the answer: the user's in default mode, the judge's in auto "
    "and auto+. Approved, each listed call runs without further review when it "
    "is made exactly as listed (the same tool and arguments), until the user's "
    "next message; any other call is reviewed as usual. Not approved, the "
    "result says why, or carries the user's words.",
    summary=plan_summary,
    validate=problems,
    plan=("string", "The plan: what will be done and why"),
    calls=({"type": "array", "items": CALL},
           "The calls the plan will make, in order"),
)
def propose_plan(plan, calls):
    return APPROVED.format(calls=listed(calls))
