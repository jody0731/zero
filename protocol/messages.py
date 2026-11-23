"""Every message a caller may send the server: the one door into ZERO.

Every app goes through it: the full screen, the plain commands (each mirrors
one message), other agents (through `zero mcp serve`) and apps to come, in any
language. A client sends one JSON object on one line and gets one line back:

    {"message": "server/hello", "protocol": 8}
        → {"ok": true, "protocol": 8, "version": "0.7.9.3", "pid": 1234, ...}
    {"message": "session/submit", "protocol": 8, "id": "<uuid>", "text": "...",
     "source": "cli", "workspace": "/a/folder", "when_busy": "steer"}
        → {"ok": true, "admitted": true, "session": "<id>",
           "handled_as": "started" | "steered" | "queued" | "reply"}
    anything wrong → {"ok": false, "error": "..."}

Messages are named noun/verb, as a tree (`session/stop`, `job/stop`,
`model/server/connect`); the commands have the same names
(`zero session stop ID`). Each message is whole in one request: it carries
all it needs, its protocol among it, and the server keeps nothing between two
of them. Each message that changes something carries an `id` the sender
picks, and the server runs an id once: sent again, the first reply comes back
(`replayed`), so a retry after a dropped connection is safe. MESSAGES below
holds each one's fields and reply as JSON Schema, the rows and records its
replies carry as named types (ROWS, RECORD, ENTRY); protocol/schema.json,
which ships, is written from it (protocol/schema.py), so an app in any
language is built against that file.

Where the server's Unix socket can tell who connects, it proves its peer;
where it can't, a request proves the user with `token`, the text of the token
file (`zero server start --json` names it, and the socket).

Two messages stay open and stream events, one JSON object a line:
`session/watch` (the records after a seq, then each new one; among them
`withdrawn`, what a stop gave back; `settled`, nothing more happens on its
own, also sent after the catch-up when it holds already; and `draft`, the
model's reply as it streams in, whole so far, ten a second at most, never
logged), and `session/list` with `watch` (the opening `sessions` snapshot,
then incremental `upsert`/`remove` diffs as sessions change, or a full
`sessions` resync if a slow watcher falls past the diff ring buffer). Each
ends with a `bye` and its reason when the server
closes it ("stopped": stay down until the user types; "restart": a newer
build, reconnect at once; "behind": the client read too slowly and the
events waiting for it passed their cap, so it watches again from its last
seq, the log holding them all; "deleted", a session's watch only: the
session was deleted; "idle": the server left idle, and a watch sent with
`keeps_server_up` false didn't hold it: the next request starts one).

A component's messages (job/…, grant/…, model/…) are described here and
handled in the component's own server part, as a tool is described in core's
registry and runs in its own folder; so a client imports this, and never a
component. Which messages only look, so that ZERO's own commands may send
them, is said by whoever serves them (server/server.py, a part's `looking`).
"""

import copy
import json
from pathlib import Path

# The variable every command ZERO runs finds its session's id in: a
# workflow's zero names that session in `for_session` (subagent/send)
SESSION_ID_ENV = "ZERO_SESSION_ID"
WORKFLOW_SOCKET_ENV = "ZERO_WORKFLOW_SOCKET"
WORKFLOW_TOKEN_ENV = "ZERO_WORKFLOW_TOKEN"

STRING = {"type": "string"}
INTEGER = {"type": "integer"}
NUMBER = {"type": "number"}
BOOLEAN = {"type": "boolean"}
OBJECT = {"type": "object"}
ANY = {}
STRINGS = {"type": "array", "items": STRING}


def maybe(schema):
    """The schema, or null."""
    return {"anyOf": [schema, {"type": "null"}]}


def one_of(*values):
    return {"enum": list(values)}


def shape(about, properties, open_to_more=False):
    """An object of these fields. `open_to_more`: it carries more that an
    app may skip (a log's record); else the server sends exactly these
    (schema.json still lets a reply grow within a protocol)."""
    found = {"type": "object", "description": about, "properties": properties}
    if open_to_more:
        found["additionalProperties"] = True
    return found


def row(name):
    """A reference to one of ROWS, as schema.json names it."""
    return {"$ref": f"#/$defs/row.{name}"}


def rows(name, about):
    return {"type": "array", "items": row(name), "description": about}


def open_rows(about):
    """Rows built from files and outside answers, not the store: what
    their description names, and more an app may skip."""
    return {"type": "array", "items": {"type": "object",
                                       "additionalProperties": True},
            "description": about}


SESSION = {**STRING, "description": "a session's id"}
# A message id the sender picks; the server runs each id once, so a retry
# after a dropped connection is safe
MESSAGE_ID = {**STRING, "minLength": 1, "maxLength": 200,
              "description": "picked by the sender, new for each message; "
                             "the same id sent again gets the first reply "
                             "and runs nothing"}
SOURCE = {**STRING, "description": "who sends it: a client's own name"}
# What every request may carry besides its message's own fields
PROTOCOL_FIELD = {**INTEGER, "description": "the protocol the request is "
                  "written in; omitted requests are read as 4 and refused "
                  "by servers with OLDEST > 4"}
TOKEN_FIELD = {**STRING, "description": "the user's token, the token file's "
               "text: needed where the socket can't tell who connects; a "
               "wrong one is refused"}
# A relay's client: ZERO's HTTP door names the process its HTTP client is,
# where the kernel says, so the server holds it to what it would hold it to
# on the socket (a ZERO command's limits)
RELAYED_FOR_PID = {**INTEGER, "minimum": 2, "description": "the process a "
                   "relay sends this for, where it can tell: the server "
                   "checks it as it checks a socket's own peer; heeded only "
                   "from the user's own client"}
EVERY_REQUEST = {"protocol": PROTOCOL_FIELD, "token": TOKEN_FIELD,
                 "relayed_for_pid": RELAYED_FOR_PID}
# A watch that leaves the server free to go when idle (a web page left open
# shouldn't keep it up all day): ended with a `bye` of "idle" when it goes
KEEPS_SERVER_UP = {**BOOLEAN, "description": "false: this watch doesn't keep "
                   "the server up; when the server leaves idle the watch ends "
                   "with bye \"idle\" (default true)"}
# What every reply to a message that changes something may say beside its
# own fields
REPLAYED = {**BOOLEAN, "description": "this id was sent before: the first "
            "reply again, and nothing ran"}
UNKNOWN_FIELDS = {**STRINGS, "description": "the request's fields this "
                  "server's protocol doesn't have, left out"}


# The user's meter of a session's context (core/timeline.py's context_meter):
# null before its first model call
CONTEXT_METER = maybe({
    "type": "object",
    "properties": {"tokens": INTEGER, "line": maybe(INTEGER),
                   "cached_share": maybe(NUMBER)},
    "description": "the context's tokens as the latest call saw them or a "
                   "compaction left them, the line it is kept under, and the "
                   "share of that call's prompt the cache served",
})

# What an ask's answers are, each with the words its box shows
CHOICE_LABELS = {
    "type": "object", "additionalProperties": STRING,
    "description": "each of `choices` (in its order) with the words the "
                   "ask's box shows for it",
}

# The kernel's own rows (component `protocol.json` files add theirs and extend
# these via `rows` / `row_extensions`): schema.json names them row.<name>
ROWS = {
    "ask": shape("a question waiting for the user's answer", {
        "kind": {"type": "string", "enum": ["permission", "question"]},
        "id": {**STRING, "description": "the ask's id: session/send's "
               "reply_to"},
        "tool": STRING,
        "command": {**STRING, "description": "the call as the ask shows it"},
        "why": {**STRING, "description": "the rule's words with the reason, "
                "as the models read them"},
        "reason": {**STRING, "description": "the reason the user reads ('' "
                   "for none)"},
        "about": {**maybe(STRING), "description": "what the ask is about, "
                  "where its rule says it"},
        "created_at": NUMBER,
        "choices": {**STRINGS, "description": "its answers, as session/send's "
                    "`choice` takes them"},
        "choice_labels": CHOICE_LABELS,
    }),
    "session": shape("a session as the list shows it", {
        "id": SESSION,
        "title": STRING,
        "source": {**STRING, "description": "who started it: a client's "
                   "name"},
        "model": STRING, "mode": STRING,
        "workspace": {**STRING, "description": "the folder its commands run "
                      "in; / is the whole machine"},
        "state": one_of("idle", "running", "waiting", "failed"),
        "created_at": NUMBER, "updated_at": NUMBER,
        "archived": BOOLEAN,
        "parent": {**maybe(SESSION), "description": "the session it sits "
                   "under (a fork, a subagent)"},
        "started_by": {**maybe(one_of("zero", "user")), "description":
                       "zero for a subagent, else the user (null: from "
                       "before 0.6.7)"},
        "unseen": {**BOOLEAN, "description": "a turn ended that the user "
                   "hasn't seen"},
        "pending": {**STRINGS, "description": "what a set changed that a "
                    "running or waiting turn still holds from before"},
        "ask": maybe(row("ask")),
    }),
}

ENTRY_REF = {"$ref": "#/$defs/entry"}
ENTRY_TYPE = {**STRING, "pattern": r"^[^/\s]+/[^/\s]+$"}
ENTRY_ID = {**STRING, "pattern": r"\S"}
ENTRY_ORIGIN = one_of("user", "system", "model", "tool")
ENTRY_TIME = {**STRING, "format": "date-time"}
ENTRY = {
    "type": "object",
    "description": "A conversation entry. Nested entries inherit origin and time.",
    "required": ["type"],
    "additionalProperties": False,
    "properties": {
        "type": ENTRY_TYPE,
        "id": maybe(ENTRY_ID),
        "origin": maybe(ENTRY_ORIGIN),
        "created_at": maybe(ENTRY_TIME),
        "links": maybe({"type": "array", "items": {
            "type": "object", "required": ["relation", "target_id"],
            "additionalProperties": False,
            "properties": {"relation": ENTRY_ID, "target_id": ENTRY_ID},
        }}),
        "fields": maybe(OBJECT),
        "entries": maybe({"type": "array", "items": ENTRY_REF}),
    },
}
TIMELINE_ENTRY = {
    "allOf": [ENTRY_REF, {
        "type": "object", "required": ["id", "origin", "created_at"],
        "properties": {"id": ENTRY_ID, "origin": ENTRY_ORIGIN,
                       "created_at": ENTRY_TIME},
        "additionalProperties": True,
    }],
}

# Protocol 8 wire record: a native timeline Entry with its log position and
# server-computed display hints (zero flat Protocol 4-7 fields at top level).
RECORD_FIELDS = {
    "seq": {**INTEGER, "description": "its place in the session's log"},
    "time": {**STRING, "description": "when it was written (ISO 8601, with "
             "its offset)"},
    "entry": {"$ref": "#/$defs/entry.timeline"},
    "parent": {**INTEGER, "description": "the seq a rewind returns to"},
    "audience": {**STRING, "description": "model: internal context, not "
                 "shown in a person's conversation"},
    "sender_name": STRING,
    "notice_source": {**STRING, "description": "what sent this notice; "
                      "it is not a person's message"},
    "call_summaries": {"type": "object", "additionalProperties": STRING,
                       "description": "each of its tool calls by id, as "
                       "an ask shows it"},
}
PUBLIC_REPLY = {
    "type": "object", "required": ["answer", "why", "next"],
    "additionalProperties": False,
    "properties": {"answer": STRING, "why": STRING, "next": {
        "type": "object", "required": ["user", "zero"],
        "additionalProperties": False, "properties": {"user": STRING, "zero": STRING},
    }},
}
RECORD = {
    "type": "object",
    "description": "a record of a session's log as a native timeline Entry",
    "properties": RECORD_FIELDS,
    "required": ["seq", "time", "entry"],
}
RECORD_ROWS = {"type": "array", "items": RECORD, "description": "the log's "
               "records"}
SESSION_LIST_STREAM = {
    "description": "opening snapshot, incremental diffs, or bye",
    "oneOf": [
        {
            "type": "object",
            "properties": {"sessions": rows("session", "the whole list")},
            "required": ["sessions"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "upsert": rows("session", "sessions added or changed"),
                "remove": {**STRINGS, "description": "session ids removed "
                           "or filtered out"},
            },
            "minProperties": 1,
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"bye": STRING},
            "required": ["bye"],
            "additionalProperties": False,
        },
    ],
}

ADMITTED = {
    "admitted": {**BOOLEAN, "description": "false: this id was sent before "
                 "and its message is already in"},
    "session": SESSION,
    "handled_as": one_of("started", "steered", "queued", "reply"),
}
CHOICES = ("yes", "session", "saved", "no")
# What a session's next turn runs with (extended by component `protocol.json`).
RUNS_WITH = shape("what a session's next turn runs with", {
    "mode": STRING, "model": STRING, "workspace": STRING,
})
# What keeps the server up, by kind: the sessions or jobs each names
BUSY_WITH = {"type": "object", "additionalProperties": STRINGS,
             "description": "what keeps the server up, by kind (turns, "
                            "queued, jobs), each the ids it names"}
# What is left once a session settles: its open ask, and what still runs
# on its own, by kind
SETTLED = {
    "type": "object",
    "properties": {"ask": maybe(row("ask"))},
    "additionalProperties": {"anyOf": [STRINGS, STRING, {"type": "null"}]},
    "description": "nothing more happens on its own: the open ask, and what "
                   "still runs on its own by kind, as each part names it "
                   "(the jobs' and the subagents' ids, work in rounds in a "
                   "line)",
}


def message(about, fields=None, required=(), reply=None, stream=None,
            changes=False, *, timeout_seconds=30, stamp_source=False):
    """One message: what it does, its fields (name → schema), which of them
    it must carry, its reply's fields (besides "ok"), for a message that
    stays open the events it streams, whether it changes something (then it
    carries an `id`, which the server runs once), its client timeout, and
    whether the client stamps its own source on outgoing requests."""
    fields = dict(fields or {})
    required = tuple(required)
    reply = dict(reply or {})
    if changes:
        fields = {"id": MESSAGE_ID, **fields}
        required = ("id", *(name for name in required if name != "id"))
        reply = {**reply, "replayed": REPLAYED}
    reply["unknown_fields"] = UNKNOWN_FIELDS
    return {
        "about": about, "fields": fields, "required": required,
        "reply": reply, "stream": stream, "changes": changes,
        "timeout_seconds": timeout_seconds, "stamp_source": stamp_source,
    }


# Extra JSON Schema `$defs` contributed by component `protocol.json` files.
DEFS = {}

# The 21 kernel messages; component messages and extensions load from
# `components/*/protocol.json` below without importing any component code.
MESSAGES = {
    "server/hello": message(
        "The server's versions and what keeps it busy; a client's first look.",
        {"protocol": {**INTEGER, "description": "the client's protocol; the "
                      "server answers in it, from the oldest it reads to its "
                      "own"}},
        reply={
            "protocol": {**INTEGER, "description": "the protocol this reply "
                         "is in: the client's, or the server's own when the "
                         "client's is newer"},
            "server_protocol": {**INTEGER, "description": "the newest "
                                "protocol the server speaks"},
            "version": {**STRING, "description": "ZERO's release the server "
                        "runs"},
            "build": STRING, "pid": INTEGER,
            "started_at": NUMBER, "busy_with": BUSY_WITH,
            "last_session": maybe(SESSION),
            "mode": STRING, "model": maybe(STRING),
            "modes": {"type": "array", "items": row("mode"),
                      "description": "the modes, least to most permissive"},
            "default_model": maybe(STRING),
            "commands": {"type": "object", "additionalProperties": STRING},
            "init_request": STRING,
        },
    ),
    "server/stop": message(
        "Stop the server; while anything runs it says what, unless forced. "
        "A restart is refused to a client of an older protocol or release.",
        {"force": BOOLEAN, "reason": one_of("stopped", "restart"),
         "version": {**STRING, "description": "the client's release: a "
                     "restart asked by an older one is refused"}},
        reply={"stopping": BOOLEAN, "busy_with": BUSY_WITH},
        changes=True,
    ),
    "session/send": message(
        "Start a session or send with a legacy client. While a question is "
        "open, this message requires its reply_to id.",
        {
            "title": {**STRING, "description": "initial title for a new session"},
            "text": STRING, "session": maybe(SESSION),
            "mode": maybe(STRING), "model": maybe(STRING), "source": SOURCE,
            "reply_to": {**maybe(STRING), "description": "the open ask's id: "
                         "this text answers it"},
            "workspace": {**maybe(STRING), "description": "a new session's "
                          "folder"},
            "when_busy": one_of("steer", "queue"),
            "choice": {**STRING, "description": "one of the open ask's "
                       "`choices`, as picked at its box"},
        },
        required=("text", "source"),
        reply=ADMITTED,
        changes=True,
    ),
    "session/continue": message(
        "A turn that ended without an answer goes on, with no new message.",
        {"session": SESSION, "source": SOURCE},
        required=("session", "source"),
        reply=ADMITTED,
        changes=True,
    ),
    "session/stop": message(
        "Stop a session's running turn; what it hadn't taken comes back.",
        {"session": SESSION, "source": SOURCE},
        required=("session", "source"),
        reply={"stopped": BOOLEAN,
               "withdrawn": {"type": "array", "items": shape(
                   "a message the turn hadn't taken", {
                       "id": STRING, "text": STRING, "source": STRING,
                   }, open_to_more=True)}},
        changes=True,
    ),
    "session/list": message(
        "The sessions, each with its open ask; with `watch`, the opening "
        "snapshot then incremental `upsert`/`remove` diffs as sessions change.",
        {
            "archived": {**maybe(BOOLEAN), "description": "only the archived "
                         "(true), the others (false), or all (null)"},
            "workspace": {**maybe(STRING), "description": "only this folder's"},
            "watch": BOOLEAN,
            "keeps_server_up": KEEPS_SERVER_UP,
        },
        reply={"sessions": rows("session", "one row a session")},
        stream=SESSION_LIST_STREAM,
    ),
    "session/deliveries": message(
        "Delivery status of the named messages in this session.",
        {"session": SESSION, "ids": {**STRINGS, "maxItems": 100}},
        required=("session", "ids"),
        reply={"deliveries": {"type": "object", "additionalProperties":
                              one_of("queued", "delivered", "done", "withdrawn")}},
    ),
    "session/read": message(
        "A session's records: those after a seq, or the newest `limit` "
        "(null: all; 0: none), before `before`; and what its next turn runs "
        "with.",
        {"session": SESSION, "after": INTEGER, "limit": maybe(INTEGER),
         "before": maybe(INTEGER)},
        required=("session",),
        reply={"records": RECORD_ROWS, "earlier_count": INTEGER,
               "runs_with": RUNS_WITH, "context": CONTEXT_METER},
    ),
    "session/watch": message(
        "A session's records after a seq, then each new one as it comes.",
        {"session": SESSION, "after": INTEGER, "limit": maybe(INTEGER),
         "keeps_server_up": KEEPS_SERVER_UP},
        required=("session",),
        stream={"record": RECORD, "settled": SETTLED,
                "draft": shape("the current model step as it streams", {
                    "step": INTEGER, "text": STRING, "reasoning": INTEGER,
                    "calls": STRINGS,
                }),
                "withdrawn": OBJECT, "earlier_count": INTEGER,
                "context": CONTEXT_METER, "bye": STRING},
    ),
    "session/mark_seen": message(
        "Mark a session read up to a seq, for the lists' news marks.",
        {"session": SESSION, "seq": {**INTEGER, "minimum": 0}},
        required=("session", "seq"),
        changes=True,
    ),
    "session/rename": message(
        "Give a session a title.",
        {"session": SESSION, "title": STRING},
        required=("session", "title"),
        changes=True,
    ),
    "session/archive": message(
        "Archive a session: out of the list, kept.",
        {"session": SESSION}, required=("session",),
        changes=True,
    ),
    "session/unarchive": message(
        "Bring an archived session back to the list.",
        {"session": SESSION}, required=("session",),
        changes=True,
    ),
    "session/delete": message(
        "Delete a session: its files to the desktop's trash or ZERO's.",
        {"session": SESSION}, required=("session",),
        reply={"trash": STRING},
        changes=True,
    ),
    "session/user_messages": message(
        "The user's own messages on a session's current branch that start a "
        "turn, newest first: where a rewind or a fork begins.",
        {"session": SESSION}, required=("session",),
        reply={"messages": {"type": "array", "items": shape(
            "one of the user's messages", {
                "seq": INTEGER, "time": STRING, "text": STRING,
            })}},
    ),
    "session/rewind": message(
        "Go back to before one of the user's messages: what came after it is "
        "left behind, kept in the log, and the files and memory stay as they "
        "are. Refused while a turn runs or waits.",
        {"session": SESSION,
         "seq": {**INTEGER, "description": "the user's message's seq, one of "
                 "session/user_messages'"}},
        required=("session", "seq"),
        reply={"rewind": {**RECORD, "description": "the rewind's record"},
               "text": {**STRING, "description": "the message's text, for "
                        "the input"}},
        changes=True,
    ),
    "session/fork": message(
        "A new session from a session's conversation, up to before one of the "
        "user's messages or all of it, its records copied as they are; the "
        "same workspace, mode and model. The session goes on untouched.",
        {"session": SESSION,
         "seq": {**INTEGER, "description": "the user's message's seq, one of "
                 "session/user_messages'; none: all of the conversation, its "
                 "last turn ended"},
         "source": SOURCE},
        required=("session",),
        reply={"session": {**row("session"), "description": "the new "
                           "session's row"},
               "text": {**STRING, "description": "the message's text, for "
                        "the new session's input; empty for all of it"}},
        changes=True,
    ),
    "session/set": message(
        "Change a session's mode, model, workspace, thinking effort or judge "
        "from its next turn.",
        {"session": SESSION, "mode": STRING, "model": STRING,
         "workspace": STRING},
        required=("session",),
        reply={"set": {"type": "object", "additionalProperties": maybe(STRING),
                       "description": "what was set, by name"},
               "next_turn": {**BOOLEAN, "description": "a running or waiting "
                             "turn holds the old values"}},
        changes=True,
    ),
    "setting/list": message(
        "ZERO's settings, each with its value, and what is wrong with "
        "config.toml (null: nothing).",
        reply={"settings": open_rows("name, group, label, default, kind, "
                                     "choices, minimum, unset_means, shown, "
                                     "needs, runs_on, value"),
               "error": maybe(STRING)},
    ),
    "setting/set": message(
        "Write one of ZERO's settings to config.toml (an app's own table, "
        "the full screen's [tui], is the app's).",
        {"name": STRING, "value": ANY}, required=("name", "value"),
        reply={"set": OBJECT},
        changes=True,
    ),
}
# A distinct wire name makes older servers refuse new submissions before admission.
MESSAGES["session/submit"] = {
    **copy.deepcopy(MESSAGES["session/send"]),
    "about": "Submit a message. Only reply_to answers a question. Other messages "
             "queue while the session waits for a reply.",
}


def protocol_files(root):
    """Return sorted `components/*/protocol.json` paths under `root`."""
    parent = Path(root) / "components"
    if not parent.is_dir():
        return []
    return [
        path for path in sorted(parent.glob("*/protocol.json"))
        if not path.parent.name.startswith((".", "_"))
    ]


def _merge_field_extension(target, field_name, ext):
    if field_name in target and isinstance(ext, dict) and isinstance(
        target[field_name], dict
    ):
        existing = target[field_name]
        if "enum" in ext and "enum" in existing:
            merged = list(existing["enum"])
            for value in ext["enum"]:
                if value not in merged:
                    merged.append(value)
            existing["enum"] = merged
            return
        if "properties" in ext and "properties" in existing:
            for key, schema in ext["properties"].items():
                existing["properties"][key] = copy.deepcopy(schema)
            return
    target[field_name] = copy.deepcopy(ext)


def load_protocol_files(root, *, defs=DEFS, rows=ROWS, messages=MESSAGES):
    """Load pure-data component `protocol.json` files into `defs`, `rows`,
    and `messages` without importing component code."""
    for path in protocol_files(root):
        data = json.loads(path.read_text(encoding="utf-8"))
        for name, schema in (data.get("defs") or {}).items():
            defs[name] = copy.deepcopy(schema)
        for name, schema in (data.get("rows") or {}).items():
            rows[name] = copy.deepcopy(schema)
        for name, spec in (data.get("messages") or {}).items():
            messages[name] = message(
                spec["about"],
                copy.deepcopy(spec.get("fields")),
                required=tuple(spec.get("required") or ()),
                reply=copy.deepcopy(spec.get("reply")),
                stream=copy.deepcopy(spec.get("stream")),
                changes=bool(spec.get("changes", False)),
                timeout_seconds=spec.get("timeout_seconds", 30),
                stamp_source=bool(spec.get("stamp_source", False)),
            )
        for row_name, extensions in (data.get("row_extensions") or {}).items():
            props = rows[row_name]["properties"]
            for field_name, ext in extensions.items():
                _merge_field_extension(props, field_name, ext)
        for msg_name, ext in (data.get("message_extensions") or {}).items():
            target = messages[msg_name]
            for field_name, schema in (ext.get("fields") or {}).items():
                _merge_field_extension(target["fields"], field_name, schema)
            if extra_req := ext.get("required"):
                req = list(target["required"])
                for field_name in extra_req:
                    if field_name not in req:
                        req.append(field_name)
                target["required"] = tuple(req)
            for reply_key, schema in (ext.get("reply") or {}).items():
                _merge_field_extension(target["reply"], reply_key, schema)
            if ext.get("stream") and isinstance(target.get("stream"), dict):
                for stream_key, schema in ext["stream"].items():
                    _merge_field_extension(target["stream"], stream_key, schema)


load_protocol_files(Path(__file__).resolve().parent.parent)

# Re-exported from component `protocol.json` definitions for callers and tests.
MODEL_FAILURE = DEFS["model_failure"]
MODEL_FAILURE_REF = {"$ref": "#/$defs/model_failure"}
COMMAND_FIELDS = DEFS["command_fields"]["properties"]

# The messages that change something, each carrying an id run once
CHANGING = tuple(name for name, spec in MESSAGES.items() if spec["changes"])


def request(name, /, **fields):
    """A request as a client sends it; ValueError for a message or a field
    the protocol doesn't have. (`name` only by place: a field may be called
    name.)"""
    if name not in MESSAGES:
        raise ValueError(f"no message {name!r} in ZERO's protocol")
    known = {**MESSAGES[name]["fields"], **EVERY_REQUEST}
    unknown = [field for field in fields if field not in known]
    if unknown:
        raise ValueError(f"{name} takes no {', '.join(unknown)}")
    return {"message": name, **fields}
