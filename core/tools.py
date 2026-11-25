"""The tool registry: a decorator declares each tool and builds its schema;
the assembly adds the components' tools (`add`); a component's door adds the
tools it finds at run time and takes them back (`add_found`, an MCP server's);
calls are checked, then run.

A tool carries generic capability traits (`traits`) and namespaced subsystem
metadata (`metadata`) queried through `has_trait`, `tools_with_trait`,
`metadata_of`, and `tools_with_metadata`. A found tool a session can call may
not be sent with its requests, for room (`deferred`): a tool whose
`found_tools` metadata declares `calls_by_name` calls it, and such a call is
the named tool's own from the start (`unwrapped`), so every check, ask and
record reads the tool that runs."""

import inspect
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from core.entries import Entry, EntryError, freeze, walk

# The tools a session may offer, by name, in the order the model reads them:
# what the assembly added (`add`), never what a module's import declared
TOOLS = {}
# The tools found at run time, after ZERO's own: what a component's door
# added (`add_found`). Replaced whole at each change, never changed in place,
# so a session reading it while a door changes it reads one or the other
FOUND = {}


@dataclass(frozen=True, slots=True)
class Result:
    """A tool's output, optional heading, and declared data."""

    output: str
    head: str = ""
    fields: Mapping = field(default_factory=dict)
    failed: bool = False
    # A tool's one requested retry passes through all checks and approval.
    # This is harness data, never part of a found tool's returned fields.
    retry_arguments: Mapping | None = None
    entries: tuple[Entry, ...] = ()

    def __post_init__(self):
        if not isinstance(self.output, str) or not isinstance(self.head, str):
            raise TypeError("tool result text must be a string")
        if self.fields is not None and not isinstance(self.fields, Mapping):
            raise EntryError("tool result fields must be an object")
        fields = dict(self.fields) if self.fields is not None else {}
        object.__setattr__(self, "fields", freeze(fields))
        if self.retry_arguments is not None:
            if not isinstance(self.retry_arguments, Mapping):
                raise EntryError("tool retry arguments must be an object")
            object.__setattr__(self, "retry_arguments", freeze(self.retry_arguments))
        if not isinstance(self.entries, (tuple, list)):
            raise EntryError("tool result entries must be an array")
        children = tuple(self.entries)
        for child in children:
            if not isinstance(child, Entry):
                raise EntryError("tool result entries must be Entry objects")
            for _ in walk(child, origin="tool"):
                pass
        object.__setattr__(self, "entries", children)

    @property
    def text(self) -> str:
        return self.head + self.output


def _copy_metadata(metadata):
    if not metadata:
        return {}
    return {
        ns: dict(val) if isinstance(val, Mapping) else val
        for ns, val in metadata.items()
    }


def tool(description, /, optional=(), fields=None, validate=None, ready=None,
         summary=None, traits=(), metadata=None, **params):
    """Declare a function as a tool the model can call: the function comes
    back marked with its declaration (`declared`), which the assembly adds to
    the registry (`add`); declaring registers nothing.

    Each keyword is one argument: name=(json_type, description), where
    json_type is a type's name or a whole JSON schema (an array's items, say).
    Arguments named in `optional` may be left out; the rest are required.
    `description`, the tool's or an argument's, may be a function of the
    session's limits and its context, written at each call.
    `fields`: the data its answer carries beside its text (a Result's
    `fields`), as MCP's outputSchema: name=(json_type, description) each.
    `validate(args)`: what is wrong with well-formed arguments, or None.
    `ready(context)`: whether a session offers the tool, from what it hands
    its tools; by default always.
    `summary(args)`: the call in a line or a few, for an ask and the judge;
    one that cuts long text takes `whole`.
    `traits`: capability trait strings (e.g. "side_by_side", "changing";
    "keeps_vault_stand_ins": its arguments reach another session as written,
    never resolved at this session's tool boundary).
    `metadata`: namespaced dicts for subsystem hooks and components.
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
            # Run with its arguments as keywords, not found at run time
            "found": False,
            # Arguments its schema doesn't list are refused
            "extra_arguments": False,
            # A found tool's (found_tool); ZERO's own are sent wherever ready
            "reachable": None,
            "output_schema": output_schema,
            "describe": describe,
            "described_arguments": described,
            "summary": summary,
            "summary_cuts": summary is not None
            and "whole" in inspect.signature(summary).parameters,
            "ready": ready,
            "validate": validate,
            "traits": frozenset(traits),
            "metadata": _copy_metadata(metadata),
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


def found_tool(name, description, parameters, run, *, ready=None, summary=None,
               validate=None, output_schema=None, extra_arguments=True,
               reachable=None, traits=(), metadata=None):
    """A tool found at run time, as `add_found` takes it: told to the model
    as `name`, `description` and `parameters` (a JSON schema of an object,
    the arguments), run as `run(args, context)`, which gives its Result.
    `ready(context)`: whether a session sends it with its requests now.
    `reachable(context)`: whether a session can call it now where `ready`
    says it isn't sent (`deferred`); None: only where it is sent.
    `extra_arguments`: whether arguments its schema doesn't list go through.
    Found tools carry the `changing` trait by default since their own side
    may change between identical calls."""
    return {
        "fn": run,
        "found": True,
        "extra_arguments": extra_arguments,
        "reachable": reachable,
        "output_schema": output_schema or {"type": "object", "properties": {}},
        "describe": None,
        "described_arguments": {},
        "summary": summary,
        "summary_cuts": summary is not None
        and "whole" in inspect.signature(summary).parameters,
        "ready": ready,
        "validate": validate,
        # What it does is its own side's, so its answer may change on its own
        "traits": frozenset({"changing", *traits}),
        "metadata": _copy_metadata(metadata),
        "schema": {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        },
    }


def add_found(entries):
    """Offer these found tools from now on (`found_tool`'s), after those
    found before; one by a name ZERO's own tools hold is left out, and
    returned by name."""
    global FOUND
    kept, clashing = dict(FOUND), []
    for entry in entries:
        name = entry["schema"]["function"]["name"]
        if name in TOOLS:
            clashing.append(name)
            continue
        kept[name] = entry
    FOUND = kept
    return clashing


def remove_found(names):
    """These found tools offered no more."""
    global FOUND
    gone = set(names)
    FOUND = {name: entry for name, entry in FOUND.items() if name not in gone}


def entry_of(name):
    """A tool's declaration by its name, ZERO's own or found; None for an
    unknown one."""
    found = TOOLS.get(name)
    return found if found is not None else FOUND.get(name)


def entries():
    """(name, declaration) of every tool, ZERO's own first, then the found."""
    return [*TOOLS.items(), *FOUND.items()]


def offered(context=None):
    """(name, entry) of each tool a session offers: those ready in its
    `context`, every one without a context."""
    for name, entry in entries():
        if (context is None or entry["ready"] is None
                or entry["ready"](context)):
            yield name, entry


def has_trait(name, trait):
    """Whether a registered or found tool declares `trait`."""
    found = entry_of(name)
    return found is not None and trait in found["traits"]


def tools_with_trait(trait, context=None):
    """(name, entry) of each offered tool declaring `trait`."""
    found = []
    for name, entry in entries():
        if trait not in entry["traits"]:
            continue
        if (context is None or entry["ready"] is None
                or entry["ready"](context)):
            found.append((name, entry))
    return found


def metadata_of(name, namespace, default=None):
    """A tool's metadata under `namespace`, or `default` if absent."""
    found = entry_of(name)
    if found is None:
        return default
    return found["metadata"].get(namespace, default)


def tools_with_metadata(namespace, context=None):
    """(name, metadata) of each offered tool carrying `namespace` metadata."""
    found = []
    for name, entry in entries():
        meta = entry["metadata"].get(namespace)
        if meta is None:
            continue
        if (context is None or entry["ready"] is None
                or entry["ready"](context)):
            found.append((name, meta))
    return found


def deferred(context=None):
    """(name, entry) of each found tool a session can call but doesn't send
    with its requests (`reachable`, not `ready`, in its `context`), in the
    order found: what a search for tools finds, and a tool that declares
    `calls_by_name` calls."""
    context = context or {}
    found = []
    for name, entry in FOUND.items():
        reachable = entry["reachable"]
        if reachable is None or not reachable(context):
            continue
        if entry["ready"] is None or entry["ready"](context):
            continue
        found.append((name, entry))
    return found


def named_call(arguments, held_in):
    """(the found tool's name, its arguments as a dict) from a call's
    arguments (JSON) to a tool that calls one by name, `held_in` its two
    arguments; or (None, what is wrong) for one that names no found tool or
    gives its arguments as something other than an object. Arguments given
    as JSON text are read as the object they hold."""
    name_argument, arguments_argument = held_in
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError as e:
        return None, f"the arguments are not valid JSON ({e})"
    if not isinstance(args, dict):
        return None, "the arguments must be a JSON object"
    name = args.get(name_argument)
    given = args.get(arguments_argument, {})
    if isinstance(given, str):
        try:
            given = json.loads(given or "{}")
        except json.JSONDecodeError:
            pass
    if not isinstance(name, str) or not name.strip():
        return None, f"{name_argument} is the name of the tool to call"
    name = name.strip()
    if name in TOOLS:
        return None, (f"{name} is among the tools sent with your requests: "
                      "call it directly")
    if name not in FOUND:
        return None, f"no tool found at run time is named {name!r}"
    if not isinstance(given, dict):
        return None, (f"{arguments_argument} is an object of {name}'s "
                      "arguments")
    return (name, given), None


def unwrapped(name, arguments):
    """For a well-formed call to a tool that calls a found tool by its name
    (`calls_by_name` in its `found_tools` metadata): (that tool's name, its
    arguments as JSON), the call it is; None for any other call."""
    entry = TOOLS.get(name)
    if entry is None:
        return None
    held_in = (entry["metadata"].get("found_tools") or {}).get("calls_by_name")
    if held_in is None:
        return None
    found, _ = named_call(arguments, held_in)
    if found is None:
        return None
    inner, given = found
    return inner, json.dumps(given, ensure_ascii=False)


def schemas(limits=None, context=None):
    """The tools as the model is told of them, under a session's `limits`;
    those not ready in its `context` are left out."""
    told = []
    for _, t in entries():
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


def summary(name, args, arguments, whole=False):
    """A checked call as an ask shows it; `whole`, as the judge reads it,
    with nothing cut (a write's content, an edit's strings)."""
    entry = entry_of(name) or {}
    describe = entry.get("summary")
    if describe is None:
        return f"{name} {arguments}"
    if entry.get("summary_cuts"):
        return describe(args, whole=whole)
    return describe(args)


# Before what is wrong with a call, in its error's text
ERROR = "Error: "
# The found tools an unknown tool's error names, the rest counted
FOUND_NAMED_MOST = 50


def check(name, arguments):
    """Return (args, None) for a well-formed call, or (None, error text)."""
    args, problem = check_problem(name, arguments)
    return args, None if problem is None else ERROR + problem


def check_problem(name, arguments):
    """Return (args, None) for a well-formed call, or (None, what is wrong
    with it), the error's text without its start (ERROR)."""
    entry = entry_of(name)
    if entry is None:
        return None, f"unknown tool {name!r}. {available_tools()}"
    params = entry["schema"]["function"]["parameters"]
    properties = params.get("properties") or {}
    required = params.get("required") or []
    expected = json.dumps({
        p: (f"<{kind_of(s)}>" if p in required
            else f"<{kind_of(s)}, optional>")
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
            missing = [p for p in required if p not in args]
            unknown = [] if entry["extra_arguments"] else [
                k for k in args if k not in properties]
            if not missing and not unknown:
                validate = entry.get("validate")
                problem = validate(args) if validate is not None else None
                if problem is None:
                    return args, None
                return None, f"bad call to {name}: {problem}."
            problem = f"missing arguments {missing}, unknown arguments {unknown}"
    return None, f"bad call to {name}: {problem}. Expected: {expected}"


def available_tools():
    """The tools there are, as an unknown tool's error names them: ZERO's
    own, then the found ones while FOUND_NAMED_MOST hold them, the rest
    counted (an MCP server can list hundreds)."""
    found = list(FOUND)
    named = [*TOOLS, *found[:FOUND_NAMED_MOST]]
    text = f"Available tools: {', '.join(named)}"
    if len(found) > FOUND_NAMED_MOST:
        text += f", and {len(found) - FOUND_NAMED_MOST} more found at run time"
    return text + "."


def kind_of(schema):
    """An argument's JSON type as the expected call names it: "any" for a
    schema that names none (a found tool's union, say)."""
    kind = schema.get("type") if isinstance(schema, dict) else None
    if isinstance(kind, list):
        return " or ".join(str(k) for k in kind)
    return kind or "any"


def run(name, args, context=None, expected_entry=None):
    """Run checked arguments through the optional execution boundary."""
    boundary = (context or {}).get("tool_boundary")
    if boundary is None:
        return run_checked(name, args, context, expected_entry)
    # A tool that hands its words to another session (`keeps_vault_stand_ins`)
    # passes the stand-ins on: that session's own tools resolve them. Display-only
    # arguments listed in `metadata["tool_boundary"]["keep_stand_ins"]` stay
    # unexpanded so secrets never become durable attachment labels or filenames.
    if not has_trait(name, "keeps_vault_stand_ins"):
        keep = set(
            (metadata_of(name, "tool_boundary") or {}).get("keep_stand_ins") or ()
        )
        try:
            if keep and isinstance(args, dict):
                to_resolve = {k: v for k, v in args.items() if k not in keep}
                resolved = boundary.resolve(to_resolve) if to_resolve else {}
                args = {
                    k: (v if k in keep else resolved[k])
                    for k, v in args.items()
                }
            else:
                args = boundary.resolve(args)
        except Exception:
            return Result("The vault stand-in could not be resolved; nothing ran.",
                          failed=True)
    answer = run_checked(name, args, context, expected_entry)
    notes = (context or {}).get("notes")
    if notes is not None:
        safe = boundary.redact(notes)
        if safe is not notes:
            notes.clear()
            notes.update(safe)
    return boundary.result(answer)


def run_checked(name, args, context=None, expected_entry=None):
    """Run a checked call: its Result. A tool that answers with text alone
    gives a Result of that text; one that fails, its error as a failed
    Result's text; fields its tool didn't declare are an error in it.

    `context`: what the session hands its tools (its sandbox); a tool gets the
    parts its function names, except a name that is one of its arguments (the
    job tool's `stop` is the model's, not the turn's stop flag). The model
    can't set them: check() refuses any argument the schema doesn't list. A
    found tool gets its arguments and the whole context (`found_tool`), and
    its fields are its own side's (an MCP tool's structured content).
    """
    entry = entry_of(name)
    if entry is None or (expected_entry is not None
                         and entry is not expected_entry):
        return Result("The tool changed while this call was checked; "
                      "nothing ran. A new call uses its current definition.",
                      failed=True)
    fn = entry["fn"]
    try:
        if entry["found"]:
            answer = fn(args, dict(context or {}))
            return answer if isinstance(answer, Result) else Result(str(answer))
        wanted = inspect.signature(fn).parameters
        arguments = entry["schema"]["function"]["parameters"]["properties"]
        extra = {k: v for k, v in (context or {}).items()
                 if k in wanted and k not in arguments}
        answer = fn(**args, **extra)
        if not isinstance(answer, Result):
            return Result(str(answer))
        declared = entry["output_schema"]["properties"]
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
