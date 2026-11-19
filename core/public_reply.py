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
    'belong in Answer alone. Give each point once, in the field that owns it. '
    'Answer is nonblank. For requested work, finish Answer at the observed '
    'result or blocker. Put the entire handoff in next.user, including a '
    'request phrased as a question or an offer like “if you send it, I can '
    'check it.” Use next.zero for distinct pending work in addition to the '
    'user’s step, or for work pending on your side alone. When the user '
    'asks for directions, advice or a reason, that is the requested answer. '
    'For “Review the comments” on a draft exported without them, Answer is '
    '“The exported draft contains no comments.” and next.user is “Export a '
    'copy with markup included and attach it here.” For “How do I share the '
    'comments?”, those directions are the Answer and Next is empty. '
    'A question to clarify the goal itself can be the Answer. '
    'If Answer is self-explanatory, leave Why empty. Use Why only for a '
    'separate fact needed to interpret Answer. If the next move is simple '
    'or obvious from Answer, leave both Next fields empty. A tool call '
    'can have no accompanying text. '
    'Internal handoffs have their own format.'
)
CORRECTION = (
    "The previous reply did not fit the public reply format. Give the reply "
    "with a nonblank Answer and all required fields. For blocked work, end "
    "Answer at the observed obstacle. Move the whole request for user input "
    "into next.user, including any offer conditional on that input. If the "
    "user asked for instructions, give them once in Answer. Leave Why empty for a "
    "self-explanatory Answer and Next empty for a simple or obvious next move. "
    "Work already done stands. "
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
