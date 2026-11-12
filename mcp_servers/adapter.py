"""The adapter between MCP and ZERO (design rule 7: where two sides change at
different times, a layer translates and states its version). The MCP side is
the MCP SDK's (a requirement, as for `zero mcp serve`), which speaks the
protocol versions PROTOCOL_VERSIONS names and settles one with each server;
this turns what a server lists and answers into what ZERO's model reads and
ZERO's parts read, and takes what it doesn't know as a line saying so.

- A tool's name for the model: `mcp__<server>__<tool>` (Codex's, Claude
  Code's, Pi's and Hermes' form), in the characters a model's API takes, at
  most 64 of them; one changed to fit ends in a short hash of the whole, so
  two never meet (Pi's and Hermes').
- Its description: the server's own words, marked as its server's (rule 17),
  after the fact that it runs in that server's process, outside the sandbox.
- Its arguments: the server's schema, which must describe an object (the
  model's API takes nothing else); arguments it doesn't list go through
  unless it says they don't, for the server to judge.
- Its answer: the text blocks as they are; an image, audio or a binary
  resource as a line naming it (the model reads text); a resource link as a
  line; the structured content as the answer's fields (rule 4: what ZERO's
  parts read), its JSON as the text where there is no text; the server's
  `isError` as a failed call. The whole between <mcp_result> tags that say
  whose it is (rule 17).
"""

import hashlib
import html
import json
import re

from mcp.types.version import KNOWN_PROTOCOL_VERSIONS

from core.asking import TO_APPROVER
from core.tools import ERROR, Result

# The MCP protocol versions this adapter speaks, oldest to newest
PROTOCOL_VERSIONS = KNOWN_PROTOCOL_VERSIONS
PREFIX = "mcp__"
# What a model's API takes in a tool's name, and how long
NAME_CHARACTERS = re.compile(r"[^A-Za-z0-9_-]")
NAME_MOST = 64
HASH_CHARS = 8
DESCRIPTION = (
    'From the MCP server "{server}", which runs as its own process, outside '
    "the sandbox; each call goes first to {approver}. In its server's words: "
    "{description}"
)
NO_DESCRIPTION = "(The server gives no description.)"
RESULT = '<mcp_result server="{server}" tool="{tool}">\n{text}\n</mcp_result>'
NOTHING = "(The tool answered with nothing.)"
# What the model reads in place of what it can't
LEFT_OUT = "[{what} ({mime}, {size:,} bytes), left out: your model reads text]"
LINK = "[A link to the resource {uri}{name}{description}]"
UNKNOWN = "[Content of type {kind}, which this ZERO doesn't read]"
RESOURCE = "[The resource {uri}]\n{text}"
# A call that didn't reach an answer: the server's error, in ZERO's words
FAILED = "the MCP server {server}'s tool {tool} gave no answer: {error}"


def tool_name(server, tool):
    """The tool's name as the model calls it."""
    whole = f"{PREFIX}{server}__{tool}"
    fitted = NAME_CHARACTERS.sub("_", whole)
    if fitted == whole and len(whole) <= NAME_MOST:
        return whole
    digest = hashlib.sha256(whole.encode()).hexdigest()[:HASH_CHARS]
    return f"{fitted[:NAME_MOST - HASH_CHARS - 1]}_{digest}"


def description(server, text):
    """The tool's description as the model reads it, marked as its
    server's."""
    words = (text or "").strip() or NO_DESCRIPTION
    return DESCRIPTION.format(server=server, approver=TO_APPROVER,
                              description=words)


def parameters(schema):
    """(the arguments' schema as the model's API takes it, whether arguments
    it doesn't list go through), or None for a schema that isn't an
    object's."""
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        return None
    told = {key: value for key, value in schema.items() if key != "$schema"}
    told["type"] = "object"
    told.setdefault("properties", {})
    return told, schema.get("additionalProperties", True) is not False


def size_of(data):
    """The bytes a base64 text holds."""
    data = data or ""
    return len(data) * 3 // 4 - data[-2:].count("=")


def block_text(block):
    """One content block of an answer, as the model reads it."""
    kind = getattr(block, "type", None)
    if kind == "text":
        return block.text
    if kind in ("image", "audio"):
        return LEFT_OUT.format(what="An image" if kind == "image" else "Audio",
                               mime=block.mime_type, size=size_of(block.data))
    if kind == "resource":
        resource = block.resource
        text = getattr(resource, "text", None)
        if text is not None:
            return RESOURCE.format(uri=resource.uri, text=text)
        return LEFT_OUT.format(
            what=f"The resource {resource.uri}",
            mime=getattr(resource, "mime_type", None) or "binary",
            size=size_of(getattr(resource, "blob", "")))
    if kind == "resource_link":
        name = f", {block.name}" if getattr(block, "name", None) else ""
        about = getattr(block, "description", None)
        return LINK.format(uri=block.uri, name=name,
                           description=f": {about}" if about else "")
    return UNKNOWN.format(kind=kind or type(block).__name__)


def answer(server, tool, result):
    """A CallToolResult as ZERO's Result: its text marked as the server's,
    its structured content as fields, its isError as failed."""
    parts = [block_text(block) for block in result.content or []]
    structured = result.structured_content
    if not any(getattr(b, "type", None) == "text" for b in result.content or []
               ) and structured is not None:
        parts.append(json.dumps(structured, indent=2, ensure_ascii=False))
    text = "\n".join(part for part in parts if part) or NOTHING
    if isinstance(structured, dict):
        fields = structured
    else:
        fields = {} if structured is None else {"result": structured}
    marked = RESULT.format(server=html.escape(server),
                           tool=html.escape(tool), text=text)
    return Result(marked, fields=fields, failed=bool(result.is_error))


def failure(server, tool, error):
    """A call that got no answer (a timeout, a server gone), as a failed
    Result."""
    return Result(ERROR + FAILED.format(server=server, tool=tool, error=error),
                  failed=True)


def reason(error):
    """An error in a line: the first of a group's, its type where its text
    says nothing."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    text = str(error).strip()
    return text or type(error).__name__
