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


@tool(
    "Ask a question and wait for its answer. The question pauses this turn "
    "until the user answers; the user may be away. The judge does not answer "
    "questions. In a helper session the question goes to its parent as a "
    "message instead; that parent answers it or asks the user. Optional "
    "choices can be selected with a key; a free-text answer is also available.",
    optional=("options",),
    needs_permission=lambda args: False,
    validate=problems,
    summary=lambda args: args["question"],
    question=("string", "The question to answer"),
    options=({"type": "array", "items": {"type": "string"}, "maxItems": 9},
             "Optional answers, in display order"),
)
def ask_user(question, options=None):
    return "The question waits for an answer."


TOOLS = (ask_user,)
