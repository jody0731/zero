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
INSTRUCTIONS = (
    'Your public reply is one JSON object in this form: '
    '{"answer":"your answer","why":"","next":{"user":"","zero":""}}. '
    'Include every field. By default only Answer has text. Leave why, '
    'next.user and next.zero empty. Greetings, small talk and routine results '
    'belong in Answer alone. A completed Answer is nonblank and stands on its '
    'own. Put a requested explanation, blocker or question there. Use Why '
    'only for a separate fact the user needs to interpret the answer. Use '
    'Next only for a concrete action still needed '
    'to finish the task. Give the user’s part first, then yours. Mention a '
    'dependency when it matters. Include each action once. A tool call '
    'can have no accompanying text. '
    'Internal handoffs have their own format.'
)
CORRECTION = (
    "The previous reply did not fit the public reply format. Give the reply "
    "with a nonblank Answer and all required fields. Work already done stands. "
    "This formatting step has no tools."
)


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
