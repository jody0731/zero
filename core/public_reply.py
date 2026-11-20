"""ZERO's public reply, shared by the model request, log and draft stream."""

import jiter


def sections(answer="", why="", user="", zero=""):
    return {"answer": answer, "why": why, "next": {"user": user, "zero": zero}}


def object_schema(example):
    properties = {key: object_schema(value) if isinstance(value, dict)
                  else {"type": "string"} for key, value in example.items()}
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


SCHEMA = {"name": "public_reply", "schema": object_schema(sections())}
INSTRUCTION_START = "Your public reply is one JSON object in this form:"
INSTRUCTION_END = "Internal handoffs have their own format."
INSTRUCTIONS = (
    f"{INSTRUCTION_START} "
    '{"answer":"your answer","why":"","next":{"user":"","zero":""}}. '
    "Include every field. Only Answer has text by default. A nonblank Answer "
    "responds directly to the user. For requested work, it gives the observed "
    "result or blocker. A goal clarification can be the Answer. Requested "
    "advice, directions and reasons belong in Answer. Why adds a fact needed "
    "to understand Answer. Next holds concrete outstanding steps that aren't "
    "obvious from Answer. Put the entire handoff in next.user, including any "
    "offer that depends on it. Use next.zero for distinct work pending on your "
    "side. Give each point once and leave unused fields empty. A tool call can "
    f"have no accompanying text. {INSTRUCTION_END}"
)
CORRECTION = (
    "The previous reply did not fit the public reply format. Use that format "
    "with the results already obtained. This formatting step has no tools."
)


def has_instructions(system):
    """Recognize the harness's final paragraph, including saved wording."""
    paragraph = system.rsplit("\n\n", 1)[-1]
    return (paragraph.startswith(INSTRUCTION_START)
            and paragraph.endswith(INSTRUCTION_END))


def parse(text, *, partial=False):
    """Read named strings, restoring display order even on an unordered server."""
    if not text:
        return None
    try:
        data = jiter.from_json(
            text.encode(), partial_mode="trailing-strings" if partial else False,
            catch_duplicate_keys=True, cache_mode="none")
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    next_steps = data.get("next", {})
    if not isinstance(next_steps, dict):
        return None
    for fields, required in ((data, {"answer", "why", "next"}),
                             (next_steps, {"user", "zero"})):
        if fields.keys() - required or (not partial and fields.keys() != required):
            return None
    values = (data.get("answer", ""), data.get("why", ""),
              next_steps.get("user", ""), next_steps.get("zero", ""))
    if not all(isinstance(value, str) for value in values):
        return None
    if not partial and not values[0].strip():
        return None
    return sections(*values)


def plain(reply):
    """Readable text for consumers that already accept a single answer string."""
    return "\n\n".join(value for value in (
        reply["answer"], reply["why"], reply["next"]["user"], reply["next"]["zero"]
    ) if value)
