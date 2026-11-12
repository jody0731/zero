"""tool_search and call_tool: the tools a session can call but doesn't send
with its requests, for room (an MCP server's whose tools don't fit its
share of the line; core's `deferred`), found by a search and called by
name.

The tools sent stay the same whatever is found (Hermes', OpenClaw's): the
tools come first in every request, so one added after a search would have
the model's server read the whole conversation again without its cache. A
found tool's definition reaches the model in the search's answer instead,
and call_tool's call is that tool's own from the start (core's
`calls_by_name`): its check, its ask, the judge's look and its record are
the found tool's, so a grant or a refusal reads the same either way.

Both are sent only while some tool is left out. tool_search's description
lists what is left out, grouped by whose it is, in LISTING_PERCENT of the
line: every name where it fits, else a source's count (Hermes and OpenClaw
saw a model call a capability missing that it hadn't been told of). It is
written with each request, as the tools are, so it is always current.
"""

import json
from html import escape

from core import tools
from core.tools import ERROR, Result, tool
from found_tools.index import COVERAGE_FROM_WORDS, document, search

# The share of the line the listing in tool_search's description takes at
# most, in characters at the token a byte ZERO's own files count
LISTING_PERCENT = 1
# The two tools' names, which each one's text names the other by (a test
# holds them to their functions')
SEARCHER = "tool_search"
CALLER = "call_tool"
# How many tools a search answers with, unless told (Hermes' 5), and at most
COUNT = 5
COUNT_MOST = 20
DESCRIPTION = (
    "Finds the tools you can call whose definitions aren't sent with each "
    "request, for room: the {count} listed below by whose they are. The "
    "query is matched against each tool's name, whose it is, its "
    "description and its arguments' names; a word that is a tool's name "
    "finds that tool first. Each tool found comes with its name, its "
    "description in its source's words and its arguments' schema; "
    "{caller} calls it by that name."
)
LISTING_HEAD = "\n\nEvery tool listed here can be called through {caller}:"
GROUP_NAMED = "\n- {source}, {count} tools: {names}"
GROUP_COUNTED = "\n- {source}, {count} tools (their names left out for room)"
FOUND = '<found_tool name="{name}">\n{description}\nIts arguments: {schema}\n</found_tool>'
FOUND_HEAD = 'For "{query}", {found} of the {count} tools not sent:'
NOTHING = (
    'Nothing found for "{query}". A tool is found when its name, '
    "description or arguments carry one of the query's two rarest words "
    "that some tool carries, and half of those words for a query of "
    "{coverage} or more. The tools not sent: {groups}."
)
NONE_LEFT_OUT = "every tool you can call is sent with each request now"
CALLER_DESCRIPTION = (
    "Calls a tool that {searcher} finds, by its name, with its arguments as "
    "an object, as its schema says. The call is that tool's own: it is "
    "checked, asked about, run and recorded as a tool sent with each "
    "request would be."
)


def left_out(context):
    """(name, entry) of each tool the session can call but doesn't send."""
    found = context.get("deferred_tools")
    return found() if found is not None else []


def groups(found):
    """[(whose, [names at the source])] of `found`, in their order."""
    by_source = {}
    for name, entry in found:
        source = entry["source"] or "found at run time"
        by_source.setdefault(source, []).append(
            entry["name_at_source"] or name)
    return list(by_source.items())


def listing(found, room):
    """The listing of `found` in `room` characters (None: no bound): each
    source with its tools' names while they fit, the rest counted."""
    text = LISTING_HEAD.format(caller=CALLER)
    for source, names in groups(found):
        named = GROUP_NAMED.format(source=source, count=len(names),
                                   names=", ".join(names))
        if room is None or len(text) + len(named) <= room:
            text += named
        else:
            text += GROUP_COUNTED.format(source=source, count=len(names))
    return text


def describe(limits, context=None):
    context = context or {}
    found = left_out(context)
    line_chars = context.get("line_chars")
    room = line_chars() * LISTING_PERCENT // 100 if line_chars else None
    return (DESCRIPTION.format(count=len(found), caller=CALLER)
            + (listing(found, room) if found else ""))


def counted(found):
    """Each source's tools not sent, counted, for a search that found
    nothing."""
    return "; ".join(f"{source}'s {len(names)}"
                     for source, names in groups(found))


def check_count(args):
    count = args.get("count")
    if count is None:
        return None
    if not isinstance(count, int) or isinstance(count, bool) or not (
            1 <= count <= COUNT_MOST):
        return f"count is a whole number from 1 to {COUNT_MOST}"
    return None


@tool(
    describe,
    optional=("count",),
    summary=lambda args: f"tool_search {args.get('query', '')}",
    ready=lambda context: bool(left_out(context)),
    needs_permission=lambda args: False,
    validate=check_count,
    fields={"tools": ({"type": "array", "items": {"type": "string"}},
                      "The names of the tools found, best first")},
    query=("string", "What the tool is to do, in a few words, or tools' "
           "names"),
    count=("integer", f"How many tools to answer with at most (1 to "
           f"{COUNT_MOST}; {COUNT} if left out)"),
)
def tool_search(query, count=None, deferred_tools=None):
    found = deferred_tools() if deferred_tools is not None else []
    if not found:
        return Result(ERROR + NONE_LEFT_OUT, failed=True)
    entries = dict(found)
    names = search([document(name, entry) for name, entry in found],
                   str(query), count or COUNT)
    if not names:
        return Result(NOTHING.format(query=query, groups=counted(found),
                                     coverage=COVERAGE_FROM_WORDS),
                      fields={"tools": []})
    parts = [FOUND_HEAD.format(query=query, found=len(names),
                               count=len(found))]
    for name in names:
        function = entries[name]["schema"]["function"]
        parts.append(FOUND.format(
            name=escape(name), description=function["description"],
            schema=json.dumps(function["parameters"], ensure_ascii=False)))
    return Result("\n".join(parts), fields={"tools": names})


def check_named_call(args):
    """What is wrong with a call by name, or None: core's reading of it."""
    _, problem = tools.named_call(json.dumps(args), ("name", "arguments"))
    return problem


@tool(
    CALLER_DESCRIPTION.format(searcher=SEARCHER),
    summary=lambda args: f"call_tool {args.get('name', '')}",
    ready=lambda context: bool(left_out(context)),
    validate=check_named_call,
    calls_by_name=("name", "arguments"),
    name=("string", "The tool's name, as the search gave it"),
    arguments=("object", "The tool's arguments, as its schema says"),
)
def call_tool(name, arguments):
    # Never runs: a well-formed call is the named tool's (core/records.py)
    return Result(ERROR + f"{name} wasn't called", failed=True)


TOOLS = (tool_search, call_tool)
