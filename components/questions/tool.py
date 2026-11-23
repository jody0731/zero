"""The question tool's declaration; its hook parks the turn."""

from core.tools import tool

NAME = "ask_user"


def problems(args):
    if not args.get("question", "").strip():
        return "question must contain text"
    options = args.get("options", [])
    if any(not item.strip() for item in options):
        return "each option must contain text"
    if len(set(options)) != len(options):
        return "options must be distinct"
    if "proposed_call" in args:
        proposal = args["proposed_call"]
        if (not isinstance(proposal, dict)
                or set(proposal) != {"tool", "arguments"}
                or not isinstance(proposal["tool"], str)
                or not isinstance(proposal["arguments"], dict)):
            return "proposed_call needs a tool name and arguments object"
        if options:
            return "proposed_call supplies its own approval choices"


PROPOSED_CALL = {
    "type": "object", "additionalProperties": False,
    "required": ["tool", "arguments"],
    "properties": {
        "tool": {"type": "string"},
        "arguments": {"type": "object"},
    },
}


@tool(
    "Ask the user a question and pause this turn for their answer. Ordinary "
    "questions in a helper session go to its parent, which can answer or ask "
    "the user. The user can select a choice or reply in their own words. "
    "With proposed_call, the app shows the exact change and approval choices. "
    "That approval comes from the user and covers the exact change once.",
    optional=("options", "proposed_call"),
    validate=problems,
    summary=lambda args: args["question"],
    traits=("asks_user",),
    metadata={"permissions": {"needs_permission": lambda args: False}},
    question=("string", "The question to answer"),
    options=({"type": "array", "items": {"type": "string"}, "maxItems": 9},
             "Choices in display order"),
    proposed_call=(PROPOSED_CALL, "The tool and full arguments for user approval"),
)
def ask_user(question, options=None, proposed_call=None):
    return "The question waits for an answer."


TOOLS = (ask_user,)
