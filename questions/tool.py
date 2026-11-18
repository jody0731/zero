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
    "Ask a question and wait for its answer. The question pauses this turn "
    "until the user answers; the user may be away. The judge does not answer "
    "questions. In a helper session the question goes to its parent as a "
    "message instead; that parent answers it or asks the user. Optional "
    "choices can be selected with a key; a free-text answer is also available. "
    "For a tool requiring the user's approval of an exact change, proposed_call "
    "contains that tool and its complete arguments. The app appends the exact "
    "proposal and supplies approval choices. Approval covers that change once; "
    "another agent's answer does not approve it.",
    optional=("options", "proposed_call"),
    asks_user=True,
    needs_permission=lambda args: False,
    validate=problems,
    summary=lambda args: args["question"],
    question=("string", "The question to answer"),
    options=({"type": "array", "items": {"type": "string"}, "maxItems": 9},
             "Optional answers, in display order"),
    proposed_call=(PROPOSED_CALL, "The exact change requiring user approval"),
)
def ask_user(question, options=None, proposed_call=None):
    return "The question waits for an answer."


TOOLS = (ask_user,)
