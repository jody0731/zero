"""Every message a caller may send the server: the one door into ZERO.

Every face goes through it: the full screen, the plain commands (each mirrors
one message), other agents (through `zero mcp serve`) and apps to come, in any
language. A client sends one JSON object on one line and gets one line back:

    {"message": "server/hello", "protocol": 4}
        → {"ok": true, "protocol": 4, "build": "<hash>", "pid": 1234, ...}
    {"message": "session/send", "id": "<uuid>", "text": "...",
     "source": "cli", "workspace": "/a/folder", "when_busy": "steer"}
        → {"ok": true, "admitted": true, "session": "<id>",
           "handled_as": "started" | "steered" | "queued" | "reply"}
    anything wrong → {"ok": false, "error": "..."}

Messages are named noun/verb, as a tree (`session/stop`, `job/stop`,
`model/server/connect`); the commands have the same names
(`zero session stop ID`). Each message is whole in one request: it carries
all it needs, and the server keeps nothing between two of them. MESSAGES below
holds each one's fields and reply as JSON Schema; protocol/schema.json, which
ships, is written from it (protocol/schema.py), so an app in any language is
built against that file.

Two messages stay open and stream events, one JSON object a line:
`session/watch` (the records after a seq, then each new one; among them
`withdrawn`, what a stop gave back; `settled`, nothing more happens on its
own, also sent after the catch-up when it holds already; and `draft`, the
model's reply as it streams in, whole so far, ten a second at most, never
logged), and `session/list` with `watch` (the list, then the whole list again
each time it changes). Each ends with a `bye` and its reason when the server
closes it ("stopped": stay down until the user types; "restart": a newer
build, reconnect at once; "behind": the client read too slowly and the
events waiting for it passed their cap, so it watches again from its last
seq, the log holding them all).

A component's messages (job/…, grant/…, model/…) are described here and
handled in the component's own server part, as a tool is described in core's
registry and runs in its own folder; so a client imports this, and never a
component. Which messages only look, so that ZERO's own commands may send
them, is said by whoever serves them (server/server.py, a part's `looking`).
"""

STRING = {"type": "string"}
INTEGER = {"type": "integer"}
NUMBER = {"type": "number"}
BOOLEAN = {"type": "boolean"}
OBJECT = {"type": "object"}
ANY = {}


def maybe(schema):
    """The schema, or null."""
    return {"anyOf": [schema, {"type": "null"}]}


def one_of(*values):
    return {"enum": list(values)}


def rows(about):
    return {"type": "array", "items": OBJECT, "description": about}


# The user's meter of a session's context (core/history.py's context_meter):
# null before its first model call
CONTEXT_METER = maybe({
    "type": "object",
    "properties": {"tokens": INTEGER, "line": maybe(INTEGER),
                   "cached_share": maybe(NUMBER)},
    "description": "the context's tokens as the latest call saw them or a "
                   "compaction left them, the line it is kept under, and the "
                   "share of that call's prompt the cache served",
})

# What a command's call carries beside its text, as its tool declares it
# (its output schema, MCP's structuredContent): the ones an app shows
COMMAND_FIELDS = {
    "exit_code": {**INTEGER, "description": "its exit code, once it ended"},
    "wall_seconds": {**NUMBER, "description": "how long it ran here"},
    "job_id": {**STRING, "description": "the background job it started, or "
               "was moved to past its timeout"},
    "timed_out": {**BOOLEAN, "description": "it ran past its timeout"},
}
# A tool call's record, as session/read's rows and session/watch's events
# carry it (`type` "tool"): what an app reads of it. A record holds more,
# which an app may skip; an older log's records have only what they had
# then (a field below that is missing says nothing)
TOOL_RECORD = {
    "type": "object",
    "description": "a tool call and its answer",
    "properties": {
        "type": {"const": "tool"},
        "tool": {**STRING, "description": "the tool's name"},
        "arguments": {**STRING, "description": "the call's arguments, as "
                      "the model wrote them (JSON)"},
        "result": {**STRING, "description": "the answer the model read"},
        "ran": {**BOOLEAN, "description": "the tool ran"},
        "denied": {**BOOLEAN, "description": "it was refused, so never ran"},
        "bad_call": {**BOOLEAN, "description": "the call was malformed"},
        "failed": {**BOOLEAN, "description": "it ran and failed (missing: "
                   "it didn't)"},
        "output_at": {**INTEGER, "description": "where the tool's own output "
                      "starts in `result`, after its head (missing: no such "
                      "split)"},
        "fields": {"type": "object", "properties": COMMAND_FIELDS,
                   "description": "what its tool declares beside its text; "
                   "a command's are listed"},
    },
}


SESSION = {**STRING, "description": "a session's id"}
# A message id the sender picks; the server admits each id once, so a retry
# after a dropped connection is safe
MESSAGE_ID = {**STRING, "minLength": 1, "maxLength": 200}
SOURCE = {**STRING, "description": "who sends it: a client's own name"}
ADMITTED = {
    "admitted": BOOLEAN,
    "session": SESSION,
    "handled_as": one_of("started", "steered", "queued", "reply"),
}
CHOICES = ("yes", "session", "saved", "no")


def message(about, fields=None, required=(), reply=None, stream=None):
    """One message: what it does, its fields (name → schema), which of them
    it must carry, its reply's fields (besides "ok"), and for a message that
    stays open, the events it streams."""
    return {
        "about": about, "fields": fields or {}, "required": tuple(required),
        "reply": reply or {}, "stream": stream,
    }


MESSAGES = {
    "server/hello": message(
        "The server's versions and what keeps it busy; a client's first look.",
        {"protocol": {**INTEGER, "description": "the client's protocol; the "
                      "server refuses another"}},
        reply={
            "protocol": INTEGER, "build": STRING, "pid": INTEGER,
            "started_at": NUMBER, "busy_with": OBJECT,
            "last_session": maybe(SESSION),
            "mode": STRING, "model": maybe(STRING),
            "modes": {"type": "array", "items": STRING},
            "default_model": maybe(STRING),
        },
    ),
    "server/stop": message(
        "Stop the server; while anything runs it says what, unless forced.",
        {"force": BOOLEAN, "reason": one_of("stopped", "restart")},
        reply={"stopping": BOOLEAN, "busy_with": OBJECT},
    ),
    "session/send": message(
        "A message to a session, or to a new one: a turn starts, or it "
        "steers or queues behind the running one, or it answers the open ask.",
        {
            "id": MESSAGE_ID, "text": STRING, "session": maybe(SESSION),
            "mode": maybe(STRING), "model": maybe(STRING), "source": SOURCE,
            "thinking_effort": {**maybe(STRING), "description": "a new session's "
                                "thinking effort: off, low, high or max"},
            "reply_to": {**maybe(STRING), "description": "the open ask's id: "
                         "this text answers it"},
            "workspace": {**maybe(STRING), "description": "a new session's "
                          "folder"},
            "when_busy": one_of("steer", "queue"),
            "choice": {**one_of(*CHOICES), "description": "one of the open "
                       "ask's choices, as picked at its box"},
            "rounds": {**BOOLEAN, "description": "the message is to be worked "
                       "on in rounds: ZERO is told, and its estimate goes to "
                       "the user"},
        },
        required=("id", "text", "source"),
        reply=ADMITTED,
    ),
    "session/continue": message(
        "A turn that ended without an answer goes on, with no new message.",
        {"id": MESSAGE_ID, "session": SESSION, "source": SOURCE},
        required=("id", "session", "source"),
        reply=ADMITTED,
    ),
    "session/stop": message(
        "Stop a session's running turn; what it hadn't taken comes back.",
        {"id": MESSAGE_ID, "session": SESSION, "source": SOURCE},
        required=("id", "session", "source"),
        reply={"stopped": BOOLEAN, "withdrawn": {"type": "array"}},
    ),
    "session/list": message(
        "The sessions, each with its open ask; with `watch`, the list again "
        "at each change (archived ones left out).",
        {
            "archived": {**maybe(BOOLEAN), "description": "only the archived "
                         "(true), the others (false), or all (null)"},
            "workspace": {**maybe(STRING), "description": "only this folder's"},
            "watch": BOOLEAN,
        },
        reply={"sessions": rows("one row a session")},
        stream={"sessions": rows("the whole list"), "bye": STRING},
    ),
    "session/read": message(
        "A session's records: those after a seq, or the newest `limit` "
        "(null: all; 0: none), before `before`; and what its next turn runs "
        "with.",
        {"session": SESSION, "after": INTEGER, "limit": maybe(INTEGER),
         "before": maybe(INTEGER)},
        required=("session",),
        reply={"records": rows("the log's records"), "earlier_count": INTEGER,
               "runs_with": {**OBJECT, "description": "what its next turn runs "
                             "with: mode, model, workspace, thinking_effort, "
                             "judge_model (null: the session's model) and "
                             "judge_thinking_effort"},
               "context": CONTEXT_METER},
    ),
    "session/watch": message(
        "A session's records after a seq, then each new one as it comes.",
        {"session": SESSION, "after": INTEGER, "limit": maybe(INTEGER)},
        required=("session",),
        stream={"record": OBJECT, "settled": OBJECT, "draft": OBJECT,
                "withdrawn": OBJECT, "earlier_count": INTEGER,
                "context": CONTEXT_METER, "bye": STRING},
    ),
    "session/mark_seen": message(
        "Mark a session read up to a seq, for the lists' news marks.",
        {"session": SESSION, "seq": {**INTEGER, "minimum": 0}},
        required=("session", "seq"),
    ),
    "session/rename": message(
        "Give a session a title.",
        {"session": SESSION, "title": STRING},
        required=("session", "title"),
    ),
    "session/archive": message(
        "Archive a session: out of the list, kept.",
        {"session": SESSION}, required=("session",),
    ),
    "session/unarchive": message(
        "Bring an archived session back to the list.",
        {"session": SESSION}, required=("session",),
    ),
    "session/delete": message(
        "Delete a session: its files to the desktop's trash or ZERO's.",
        {"session": SESSION}, required=("session",),
        reply={"trash": STRING},
    ),
    "session/compact": message(
        "Compact a session's context: at once when it rests, before the next "
        "step when its turn runs.",
        {"session": SESSION,
         "instructions": {**maybe(STRING), "description": "the user's words "
                          "on what the handoff keeps, beside its fixed form; "
                          "with them a handoff is written"}},
        required=("session",),
        reply={"compacting": {"type": "string",
                              "enum": ["now", "before_next_step"]}},
    ),
    "session/user_messages": message(
        "The user's own messages on a session's current branch that start a "
        "turn, newest first: where a rewind or a fork begins.",
        {"session": SESSION}, required=("session",),
        reply={"messages": rows("seq, time and text of each")},
    ),
    "session/rewind": message(
        "Go back to before one of the user's messages: what came after it is "
        "left behind, kept in the log, and the files and memory stay as they "
        "are. Refused while a turn runs or waits.",
        {"session": SESSION,
         "seq": {**INTEGER, "description": "the user's message's seq, one of "
                 "session/user_messages'"}},
        required=("session", "seq"),
        reply={"rewind": {**OBJECT, "description": "the rewind's record"},
               "text": {**STRING, "description": "the message's text, for "
                        "the input"}},
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
        reply={"session": {**OBJECT, "description": "the new session's row"},
               "text": {**STRING, "description": "the message's text, for "
                        "the new session's input; empty for all of it"}},
    ),
    "session/set": message(
        "Change a session's mode, model, workspace, thinking effort or judge "
        "from its next turn.",
        {"session": SESSION, "mode": STRING, "model": STRING,
         "workspace": STRING, "thinking_effort": STRING,
         "judge_model": {**STRING, "description": "empty: the session's own "
                         "model"},
         "judge_thinking_effort": STRING},
        required=("session",),
        reply={"set": OBJECT, "next_turn": BOOLEAN},
    ),
    "session/search": message(
        "Search what every session's log holds, running or done: with query "
        "alone, the sessions whose records match, best first, each with its "
        "best record; with query and session, that session's matching "
        "records; with session and around, the records near one, whole.",
        {
            "query": {**STRING, "description": "words a record holds, each "
                      "whole; OR, \"a phrase\" and word* as FTS5 reads them"},
            "session": {**STRING, "description": "a session's id or the start "
                        "of it"},
            "around": {**INTEGER, "description": "a record's seq in that "
                       "session"},
            "count": {**INTEGER, "description": "how many sessions, or "
                      "records, at most (10); with no query, how many on each "
                      "side (5)"},
        },
        reply={
            "query": maybe(STRING),
            "session": {**OBJECT, "description": "the session searched or "
                        "read: id, title, workspace, state, mode, archived, "
                        "updated_at"},
            "sessions": rows("the matching sessions, best first: a session's "
                             "fields, record_count and its best record"),
            "records": rows("records: seq, kind (user, notice, reply, call, "
                            "handoff), a call's tool or a notice's source, "
                            "time, and a snippet (a match marked « ») or, near "
                            "around, the whole text"),
            "session_count": INTEGER, "record_count": INTEGER,
            "matched_by": {**one_of("every word", "any word"),
                           "description": "what the records hold: every word, "
                           "or, when no record does, any of them"},
            "around": INTEGER, "earlier_count": INTEGER, "later_count": INTEGER,
        },
    ),
    "job/list": message(
        "A session's background jobs, ended ones too; with none, every "
        "running job.",
        {"session": SESSION},
        reply={"jobs": rows("one row a job, its state in words in "
                            "`state_text`")},
    ),
    "job/stop": message(
        "Stop a background job.",
        {"job": STRING}, required=("job",),
        reply={"stopped": BOOLEAN},
    ),
    "rounds/list": message(
        "A session's work in rounds, ended runs too; with none, every open "
        "one.",
        {"session": SESSION},
        reply={"rounds": rows("one row a run, in a line in `summary`")},
    ),
    "rounds/stop": message(
        "Stop work in rounds: no next round starts; a round under way ends its "
        "turn.",
        {"rounds": STRING}, required=("rounds",),
        reply={"stopped": BOOLEAN},
    ),
    "grant/list": message(
        "The grants that hold in a session: what an ask's \"don't ask "
        "again\" allowed.",
        {"session": SESSION}, required=("session",),
        reply={"grants": rows("id, by, scope, grant, description")},
    ),
    "grant/forget": message(
        "Take a grant back.",
        {"session": SESSION, "grant": STRING},
        required=("session", "grant"),
        reply={"forgot": STRING},
    ),
    "model/list": message(
        "Every server the server can reach, with its models by the names a "
        "session takes, or why it lists none.",
        reply={"servers": rows("one row a server")},
    ),
    "model/features": message(
        "What a model honours of its API's features (thinking_off, "
        "thinking_on, strict_json, json_mode), as measured: each true or "
        "false, null where never measured. Measured now when none are kept "
        "or `test` is true, a few small calls to the model.",
        {"model": STRING, "test": BOOLEAN}, required=("model",),
        reply={"model": STRING, "api": STRING, "measured_at": STRING,
               "features": OBJECT},
    ),
    "model/server/list": message(
        "Every model server the server knows, whether its key is there, the "
        "default model, and why a session on it couldn't start (null: it can).",
        reply={"servers": rows("name, title, url, key_state, needs_key, "
                               "user_added, in_catalog, context_window, "
                               "models"),
               "default": maybe(STRING),
               "not_ready_reason": maybe(STRING)},
    ),
    "model/server/probe": message(
        "A server's models, for a pick list. A typed url takes the typed key "
        "or none; a name alone, its own url and stored key.",
        {"name": STRING, "url": STRING, "key": STRING},
        reply={"url": STRING, "models": rows("id, context_window")},
    ),
    "model/server/connect": message(
        "A server's model tested with a call of a few tokens, then saved: "
        "the key in keys.toml, the server in models.toml; the first model "
        "connected "
        "becomes the default.",
        {"name": STRING, "url": STRING, "key": STRING, "model": STRING,
         "context_window": {**INTEGER, "minimum": 1}, "title": STRING,
         "default": BOOLEAN},
        required=("name", "model"),
        reply={"model": STRING, "default": BOOLEAN},
    ),
    "model/server/disconnect": message(
        "A server's key and the user's lines for it taken out.",
        {"name": STRING}, required=("name",),
        reply={"removed": STRING, "default_unset": BOOLEAN},
    ),
    "setting/list": message(
        "ZERO's settings, each with its value, and what is wrong with "
        "config.toml (null: nothing).",
        reply={"settings": rows("name, group, label, default, kind, choices, "
                                "minimum, unset_means, shown, needs, runs_on, "
                                "value"),
               "error": maybe(STRING)},
    ),
    "setting/set": message(
        "Write one of ZERO's settings to config.toml (a face's own table, "
        "the full screen's [tui], is the face's).",
        {"name": STRING, "value": ANY}, required=("name", "value"),
        reply={"set": OBJECT},
    ),
}


def request(name, /, **fields):
    """A request as a client sends it; ValueError for a message or a field
    the protocol doesn't have. (`name` only by place: a field may be called
    name.)"""
    if name not in MESSAGES:
        raise ValueError(f"no message {name!r} in ZERO's protocol")
    unknown = [field for field in fields if field not in MESSAGES[name]["fields"]]
    if unknown:
        raise ValueError(f"{name} takes no {', '.join(unknown)}")
    return {"message": name, **fields}
