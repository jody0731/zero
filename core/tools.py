"""The tool registry: a decorator builds each schema; calls are checked, then run."""

import inspect
import json
from dataclasses import dataclass

TOOLS = {}


@dataclass(frozen=True)
class Grant:
    """What "don't ask again" grants for a tool's calls. `field`: the list a
    grant keeps its items under (the user's saved file names it too);
    `of(args)`: a call's items, or None for a call no grant may cover;
    `words(grant)`: a grant in a few words, for the ask and the remark."""

    field: str
    of: object
    words: object


def tool(description, optional=(), summary=None, ready=None,
         needs_permission=None, validate=None, changing=False, starts_job=None,
         crossing=None, grant=None, written_files=None, **params):
    """Register a function as a tool the model can call.

    Each keyword is one argument: name=(json_type, description), where
    json_type is a type's name or a whole JSON schema (an array's items, say).
    Arguments named
    in `optional` may be left out; the rest are required. `description` may be
    a function of the session's limits and its context (bash's timeout, how
    deep subagents go), written at each call.
    `summary(args)`: the call in a line or a few, for an ask and the judge
    (bash's command, a write's path and its content); by default the raw
    arguments. One that cuts long text for an ask's screen takes `whole`,
    which the judge passes to read it uncut. `ready(context)`: whether a
    session offers the tool, from what it hands its tools (web_search needs a
    key); by default always.
    `needs_permission(args)`: False for a call that changes nothing on the
    machine (a job's list, a read, a search, a fetch from the internet), which
    auto+ lets run unasked (permissions/approval.py, Permissions); by default
    every call needs it.
    `validate(args)`: what is wrong with well-formed arguments, or None; a
    call it faults is refused before any check or ask (a plan's calls).
    `changing`: True for a tool whose answer changes on its own (a running
    job's output), so the same call made again runs again.
    `starts_job(args)`: True for a call that starts a background job, which
    counts against the session's cap on running jobs; by default none does.
    `crossing(args)`: what a call crosses that is asked about first (leaving
    the sandbox, reaching the local network), or None: a dict of its "rule",
    the one sentence that says what is crossed ("fact") and the model's
    reason ("why"). Whoever answers the ask (the user, the consent check, the
    judge) reads the same sentence (`as_clause` puts it inside theirs); the
    tool says what is crossed, in its words.
    `grant`: a Grant, what "don't ask again" grants for its calls; None
    offers none.
    `written_files(args)`: the files a call writes, as the call names them (a
    write's or an edit's path), so files no tool may change can be refused by
    whoever keeps them; by default none.
    """

    def register(fn):
        properties = {
            name: {
                **(json_type if isinstance(json_type, dict)
                   else {"type": json_type}),
                "description": text,
            }
            for name, (json_type, text) in params.items()
        }
        describe = description if callable(description) else None
        TOOLS[fn.__name__] = {
            "fn": fn,
            "describe": describe,
            "summary": summary,
            "summary_cuts": summary is not None
            and "whole" in inspect.signature(summary).parameters,
            "ready": ready,
            "needs_permission": needs_permission,
            "validate": validate,
            "changing": changing,
            "starts_job": starts_job,
            "crossing": crossing,
            "grant": grant,
            "written_files": written_files,
            "schema": {
                "type": "function",
                "function": {
                    "name": fn.__name__,
                    "description": describe({}) if describe else description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": [p for p in params if p not in optional],
                    },
                },
            },
        }
        return fn

    return register


def schemas(limits=None, context=None):
    """The tools as the model is told of them, under a session's `limits`;
    those not ready in its `context` are left out."""
    told = []
    for t in TOOLS.values():
        if t["ready"] is not None and not t["ready"](context or {}):
            continue
        schema = t["schema"]
        if t["describe"] is not None:
            text = t["describe"](limits or {}, context or {})
            schema = {**schema, "function": {**schema["function"], "description": text}}
        told.append(schema)
    return told


def changing(name):
    """Whether the tool's answer changes on its own; False for an unknown one."""
    found = TOOLS.get(name)
    return found is not None and found["changing"]


def starts_job(name, args):
    """Whether the call starts a background job; False for an unknown tool."""
    found = TOOLS.get(name)
    return found is not None and found["starts_job"] is not None and bool(
        found["starts_job"](args)
    )


def crossing(name, args):
    """What the call crosses (its tool's `crossing`), or None."""
    found = TOOLS.get(name)
    if found is None or found["crossing"] is None:
        return None
    return found["crossing"](args)


def written_files(name, args):
    """The files the call writes (its tool's `written_files`); [] for none."""
    found = TOOLS.get(name)
    if found is None or found["written_files"] is None:
        return []
    return list(found["written_files"](args) or [])


def as_clause(fact):
    """A crossing's fact as part of another sentence: its first letter small,
    without its full stop."""
    return fact[:1].lower() + fact[1:].rstrip(".")


def declared_grant(name):
    """The tool's Grant, or None for a tool that offers none."""
    found = TOOLS.get(name)
    return None if found is None else found["grant"]


def grant_fields():
    """The lists a grant may keep, as the tools declare them."""
    fields = []
    for t in TOOLS.values():
        if t["grant"] is not None and t["grant"].field not in fields:
            fields.append(t["grant"].field)
    return fields


def needs_permission(name, args):
    """Whether a checked call needs permission (`tool`'s `needs_permission`);
    True for a tool that says nothing and for an unknown one."""
    check = TOOLS.get(name, {}).get("needs_permission")
    return True if check is None else bool(check(args))


def summary(name, args, arguments, whole=False):
    """A checked call as an ask shows it; `whole`, as the judge reads it,
    with nothing cut (a write's content, an edit's strings)."""
    entry = TOOLS.get(name, {})
    describe = entry.get("summary")
    if describe is None:
        return f"{name} {arguments}"
    if entry.get("summary_cuts"):
        return describe(args, whole=whole)
    return describe(args)


def check(name, arguments):
    """Return (args, None) for a well-formed call, or (None, error text)."""
    if name not in TOOLS:
        available = ", ".join(TOOLS)
        return None, f"Error: unknown tool {name!r}. Available tools: {available}."
    params = TOOLS[name]["schema"]["function"]["parameters"]
    properties = params["properties"]
    expected = json.dumps({
        p: f"<{s['type']}>" if p in params["required"] else f"<{s['type']}, optional>"
        for p, s in properties.items()
    })
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError as e:
        problem = f"the arguments are not valid JSON ({e})"
    else:
        if not isinstance(args, dict):
            problem = "the arguments must be a JSON object"
        else:
            missing = [p for p in params["required"] if p not in args]
            unknown = [k for k in args if k not in params["properties"]]
            if not missing and not unknown:
                validate = TOOLS[name].get("validate")
                problem = validate(args) if validate is not None else None
                if problem is None:
                    return args, None
                return None, f"Error: bad call to {name}: {problem}."
            problem = f"missing arguments {missing}, unknown arguments {unknown}"
    return None, f"Error: bad call to {name}: {problem}. Expected: {expected}"


def run(name, args, context=None):
    """Run a checked call. A tool that fails returns its error as text.

    `context`: what the session hands its tools (its sandbox); a tool gets the
    parts its function names, except a name that is one of its arguments (the
    job tool's `stop` is the model's, not the turn's stop flag). The model
    can't set them: check() refuses any argument the schema doesn't list.
    """
    fn = TOOLS[name]["fn"]
    wanted = inspect.signature(fn).parameters
    arguments = TOOLS[name]["schema"]["function"]["parameters"]["properties"]
    extra = {k: v for k, v in (context or {}).items()
             if k in wanted and k not in arguments}
    try:
        return str(fn(**args, **extra))
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


def normalise(arguments):
    """Same call, same key: ignore whitespace and key order in the JSON."""
    try:
        return json.dumps(json.loads(arguments or "{}"), sort_keys=True)
    except json.JSONDecodeError:
        return arguments
