"""The tool registry: a decorator declares each tool and builds its schema;
the assembly adds the components' tools (`add`); calls are checked, then run."""

import inspect
import json
from dataclasses import dataclass

# The tools a session may offer, by name, in the order the model reads them:
# what the assembly added (`add`), never what a module's import declared
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


class Result(str):
    """A tool's answer (MCP's tool result): the text the model reads, `head`
    then `output`, so a caller that takes text takes it as it is; `head` is
    what a tool puts above its output (bash's status and time), so a screen
    can show the output alone. `fields`: data beside the text, as the tool
    declares them (its `fields`), which others read in place of its words;
    `failed`: the call didn't do what it was asked (MCP's isError)."""

    def __new__(cls, output, head="", fields=None, failed=False):
        answer = super().__new__(cls, head + output)
        answer.output, answer.head = output, head
        answer.fields = dict(fields or {})
        answer.failed = failed
        return answer

    @property
    def text(self):
        return str(self)


def tool(description, optional=(), summary=None, ready=None,
         needs_permission=None, validate=None, changing=False, starts_job=None,
         crossing=None, grant=None, written_files=None, means=None,
         fields=None, result_summary=None, reads_records=False,
         reads_in_parts=None, **params):
    """Declare a function as a tool the model can call: the function comes
    back marked with its declaration (`declared`), which the assembly adds to
    the registry (`add`); declaring registers nothing.

    Each keyword is one argument: name=(json_type, description), where
    json_type is a type's name or a whole JSON schema (an array's items, say).
    Arguments named
    in `optional` may be left out; the rest are required. `description`, the
    tool's or an argument's, may be a function of the session's limits and
    its context (bash's timeout, how deep subagents go, a search's default
    count), written at each call.
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
    `starts_job`: the argument that, true, starts the call as a background
    job, which counts against the session's cap on running jobs (bash's
    `background`); by default none does. Texts about jobs name it as the
    tool declares it (`job_starter`).
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
    `means`: for a tool that is a means of carrying out a task (a background
    job, rounds, a subagent), what the means is for, in a line the system
    message lists beside the others' (`means_lines`); or a function of the
    session's context that returns the line, None where the session hasn't
    the means. By default the tool is none.
    `fields`: the data its answer carries beside its text (a Result's
    `fields`), as MCP's outputSchema: name=(json_type, description) each;
    others read these, never the text. `result_summary(fields)`: a call
    that ran, as its fields say what it came to, in a few words for the
    judge's view (a command that became a job), or None.
    `reads_records`: True for the tool that reads a session's log by its
    record numbers (`record_reader`), so a text naming records can say how
    to read them. `reads_in_parts`: the two arguments that read a file in
    parts, where to start and how many lines (`part_reader`), so a text
    naming a saved file can say how to read it.
    """

    def declare(fn):
        taken = inspect.signature(fn).parameters
        missing = [name for name in params if name not in taken]
        if missing:
            raise TypeError(
                f"{fn.__name__} declares arguments it doesn't take: {missing}")
        properties = {
            name: {
                **(json_type if isinstance(json_type, dict)
                   else {"type": json_type}),
                # Without a session's limits, as the tool's own text is
                "description": text({}, {}) if callable(text) else text,
            }
            for name, (json_type, text) in params.items()
        }
        # The arguments whose text is written at each call
        described = {name: text for name, (_, text) in params.items()
                     if callable(text)}
        describe = description if callable(description) else None
        output_schema = {
            "type": "object",
            "properties": {
                name: {**(kind if isinstance(kind, dict) else {"type": kind}),
                       "description": text}
                for name, (kind, text) in (fields or {}).items()
            },
            "additionalProperties": False,
        }
        fn.declared = {
            "fn": fn,
            "output_schema": output_schema,
            "result_summary": result_summary,
            "reads_records": reads_records,
            "reads_in_parts": reads_in_parts,
            "describe": describe,
            "described_arguments": described,
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
            "means": means,
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

    return declare


def add(*declared):
    """Offer these tools from now on, in this order after those added before:
    each a function `tool` declared. Adding the same tool again changes
    nothing; another one by a known name is an error."""
    for fn in declared:
        entry = fn.declared
        name = entry["schema"]["function"]["name"]
        known = TOOLS.get(name)
        if known is entry:
            continue
        if known is not None:
            raise ValueError(f"tool {name!r} declared twice")
        TOOLS[name] = entry


def means_lines(context=None):
    """What each means of carrying out a task is for, a line a means, as the
    tools a session offers say it (`means`), in the order registered: only
    the means the session has, from what it hands its tools (`context`)."""
    context = context or {}
    lines = []
    for t in TOOLS.values():
        line = t["means"]
        if line is None or (t["ready"] is not None and not t["ready"](context)):
            continue
        if callable(line):
            line = line(context)
        if line:
            lines.append(line)
    return lines


def offered(context=None):
    """(name, entry) of each tool a session offers: those ready in its
    `context`, every one without a context."""
    for name, entry in TOOLS.items():
        if (context is None or entry["ready"] is None
                or entry["ready"](context)):
            yield name, entry


def job_starter(context=None):
    """(the tool, its argument) that starts a call as a background job, as
    the tool declares it (`starts_job`); None where no tool offered does."""
    return next(((name, entry["starts_job"])
                 for name, entry in offered(context) if entry["starts_job"]),
                None)


def record_reader(context=None):
    """The tool that reads a session's log by its record numbers
    (`reads_records`), or None where no tool offered does."""
    return next((name for name, entry in offered(context)
                 if entry["reads_records"]), None)


def part_reader(context=None):
    """(the tool, where to start, how many lines) that reads a file in parts
    (`reads_in_parts`), or None where no tool offered does."""
    return next(((name, *entry["reads_in_parts"])
                 for name, entry in offered(context)
                 if entry["reads_in_parts"]), None)


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
        if t["described_arguments"]:
            function = schema["function"]
            properties = dict(function["parameters"]["properties"])
            for name, text in t["described_arguments"].items():
                properties[name] = {
                    **properties[name],
                    "description": text(limits or {}, context or {}),
                }
            schema = {**schema, "function": {
                **function,
                "parameters": {**function["parameters"], "properties": properties},
            }}
        told.append(schema)
    return told


def changing(name):
    """Whether the tool's answer changes on its own; False for an unknown one."""
    found = TOOLS.get(name)
    return found is not None and found["changing"]


def starts_job(name, args):
    """Whether the call starts a background job (its tool's `starts_job`
    argument is true); False for an unknown tool."""
    found = TOOLS.get(name)
    return (found is not None and found["starts_job"] is not None
            and args.get(found["starts_job"]) is True)


def result_summary(name, fields):
    """What a call that ran came to, from its result's fields, in its
    tool's words (`result_summary`), or None."""
    found = TOOLS.get(name)
    if not fields or found is None or found["result_summary"] is None:
        return None
    return found["result_summary"](fields)


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


# Before what is wrong with a call, in its error's text
ERROR = "Error: "


def check(name, arguments):
    """Return (args, None) for a well-formed call, or (None, error text)."""
    args, problem = check_problem(name, arguments)
    return args, None if problem is None else ERROR + problem


def check_problem(name, arguments):
    """Return (args, None) for a well-formed call, or (None, what is wrong
    with it), the error's text without its start (ERROR)."""
    if name not in TOOLS:
        available = ", ".join(TOOLS)
        return None, f"unknown tool {name!r}. Available tools: {available}."
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
                return None, f"bad call to {name}: {problem}."
            problem = f"missing arguments {missing}, unknown arguments {unknown}"
    return None, f"bad call to {name}: {problem}. Expected: {expected}"


def run(name, args, context=None):
    """Run a checked call: its Result. A tool that answers with text alone
    gives a Result of that text; one that fails, its error as a failed
    Result's text; fields its tool didn't declare are an error in it.

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
        answer = fn(**args, **extra)
        if not isinstance(answer, Result):
            return Result(str(answer))
        declared = TOOLS[name]["output_schema"]["properties"]
        unknown = sorted(set(answer.fields) - set(declared))
        if unknown:
            raise ValueError(f"{name} gave fields it doesn't declare: {unknown}")
        return answer
    except Exception as e:
        return Result(f"{ERROR}{type(e).__name__}: {e}", failed=True)


def normalise(arguments):
    """Same call, same key: ignore whitespace and key order in the JSON."""
    try:
        return json.dumps(json.loads(arguments or "{}"), sort_keys=True)
    except json.JSONDecodeError:
        return arguments
