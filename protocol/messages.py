"""Every message a caller may send the server: the one door into ZERO.

Every app goes through it: the full screen, the plain commands (each mirrors
one message), other agents (through `zero mcp serve`) and apps to come, in any
language. A client sends one JSON object on one line and gets one line back:

    {"message": "server/hello", "protocol": 5}
        → {"ok": true, "protocol": 5, "version": "0.7.6.1", "pid": 1234, ...}
    {"message": "session/send", "protocol": 5, "id": "<uuid>", "text": "...",
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
replies carry as named types (ROWS, RECORDS); protocol/schema.json, which
ships, is written from it (protocol/schema.py), so an app in any language is
built against that file.

Where the server's Unix socket can tell who connects, it proves its peer;
where it can't, a request proves the user with `token`, the text of the token
file (`zero server start --json` names it, and the socket).

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
seq, the log holding them all; "deleted", a session's watch only: the
session was deleted).

A component's messages (job/…, grant/…, model/…) are described here and
handled in the component's own server part, as a tool is described in core's
registry and runs in its own folder; so a client imports this, and never a
component. Which messages only look, so that ZERO's own commands may send
them, is said by whoever serves them (server/server.py, a part's `looking`).
"""

# The variable every command ZERO runs finds its session's id in: a
# workflow's zero names that session in `for_session` (subagent/send)
SESSION_ID_ENV = "ZERO_SESSION_ID"

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
                  "written in; none: 4, the last before requests named it"}
TOKEN_FIELD = {**STRING, "description": "the user's token, the token file's "
               "text: needed where the socket can't tell who connects; a "
               "wrong one is refused"}
EVERY_REQUEST = {"protocol": PROTOCOL_FIELD, "token": TOKEN_FIELD}
# What every reply to a message that changes something may say beside its
# own fields
REPLAYED = {**BOOLEAN, "description": "this id was sent before: the first "
            "reply again, and nothing ran"}
UNKNOWN_FIELDS = {**STRINGS, "description": "the request's fields this "
                  "server's protocol doesn't have, left out"}


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

# What an ask's answers are, each with the words its box shows
CHOICE_LABELS = {
    "type": "object", "additionalProperties": STRING,
    "description": "each of `choices` (in its order) with the words the "
                   "ask's box shows for it",
}

# The rows replies carry, each built from the store by one function in the
# server (server/replies.py, a service's own rows), so a column renamed
# there changes no reply: schema.json names them row.<name>
ROWS = {
    "ask": shape("a question waiting for the user's answer", {
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
        "jobs": {**INTEGER, "description": "its running background jobs"},
        "rounds": {**maybe(STRING), "description": "its open work in rounds, "
                   "in a line"},
    }),
    "mode": shape("one of the modes a session runs in", {
        "name": STRING,
        "about": {**STRING, "description": "its sandbox and who answers its "
                  "asks, in a line"},
        "in_cycle": {**BOOLEAN, "description": "a key that steps through the "
                     "modes lands on it (false: chosen by its name only)"},
    }),
    "job": shape("a background job", {
        "id": STRING, "session": SESSION, "command": STRING,
        "pid": INTEGER,
        "state": one_of("running", "ended", "stopped", "lost"),
        "exit_code": maybe(INTEGER),
        "started_at": NUMBER, "ended_at": maybe(NUMBER),
        "due_at": {**maybe(NUMBER), "description": "when the model's "
                   "estimate runs out"},
        "stopped_by": maybe(one_of("model", "user")),
        "state_text": {**STRING, "description": "its state in words, as the "
                       "model reads it"},
    }),
    "rounds": shape("a session's work in rounds", {
        "id": STRING, "session": SESSION, "objective": STRING,
        "state": STRING,
        "round_count": {**INTEGER, "description": "the round under way, or "
                        "the last"},
        "estimate_rounds": INTEGER, "estimate_minutes": NUMBER,
        "estimate_dollars": NUMBER,
        "spent_minutes": NUMBER, "spent_dollars": NUMBER,
        "last_status": maybe(one_of("going_on", "done", "stuck")),
        "started_at": NUMBER, "ended_at": maybe(NUMBER),
        "summary": {**STRING, "description": "the run in a line"},
    }),
}

# A log record's own fields, which every record has; a record holds more,
# which an app may skip, and an older log's records have only what they had
# then (a field below that is missing says nothing)
RECORD_FIELDS = {
    "seq": {**INTEGER, "description": "its place in the session's log"},
    "type": STRING,
    "time": {**STRING, "description": "when it was written (ISO 8601, with "
             "its offset)"},
}
# What an app reads of each kind of record, as session/read's rows and
# session/watch's events carry them: schema.json names them record.<type>
RECORDS = {
    "session": {
        "note": {**STRING, "description": "a line the session's start "
                 "carries (a fork's)"},
    },
    "user": {
        "text": STRING, "source": maybe(STRING),
        "inbox": {**STRING, "description": "the message's id"},
        "steer": {**BOOLEAN, "description": "sent while a turn ran"},
        "continue": {**BOOLEAN, "description": "a turn went on with no new "
                     "message"},
    },
    "assistant": {
        "message": {**OBJECT, "description": "the model's message: content "
                    "and tool_calls"},
        "call_summaries": {"type": "object", "additionalProperties": STRING,
                           "description": "each of its tool calls by id, as "
                           "an ask shows it"},
        "prompt_tokens": maybe(INTEGER), "cached_tokens": maybe(INTEGER),
        "completion_tokens": maybe(INTEGER),
    },
    "tool": {
        "tool": {**STRING, "description": "the tool's name"},
        "arguments": {**STRING, "description": "the call's arguments, as "
                      "the model wrote them (JSON); for a call made through "
                      "another tool, those it gave for this one"},
        "called_through": {**STRING, "description": "the tool the model "
                           "called, which called this one by its name "
                           "(missing: it called this one itself)"},
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
        "saved_path": {**STRING, "description": "the file its whole result "
                       "was saved in, past its share of the context "
                       "(missing: none)"},
    },
    "ask": {
        "ask": {**STRING, "description": "the ask's id"},
        "tool": STRING, "command": STRING, "why": STRING, "reason": STRING,
        "about": STRING,
        "choices": STRINGS,
        "choice_labels": CHOICE_LABELS,
    },
    "answer": {
        "ask": STRING, "yes": BOOLEAN, "source": maybe(STRING),
    },
    "note": {
        "kind": STRING, "text": STRING,
    },
    "switch": {
        "from": OBJECT, "to": {**OBJECT, "description": "what changed, by "
                               "name, to its new value"},
    },
    "compaction": {
        "reason": STRING, "tokens_before": INTEGER, "tokens_after": INTEGER,
        "summary": maybe(STRING),
        "instructions": {**OBJECT, "description": "what its askers said the "
                         "handoff keeps, by who asked"},
    },
    "compaction_failed": {"reason": STRING, "error": STRING},
    "nothing_to_compact": {"reason": STRING},
    "rewind": {
        "left_from": {**INTEGER, "description": "the first record it left "
                      "behind"},
        "left_to": {**INTEGER, "description": "the last record it left "
                    "behind"},
    },
    "turn_end": {
        "outcome": STRING, "answer": maybe(STRING),
        "detail": maybe(STRING), "error": maybe(STRING),
        "seconds": NUMBER,
    },
}
# What an app reads of a tool call's record (record.tool), kept under its
# old name for the readers of 7.1's
TOOL_RECORD = shape("a tool call and its answer", {
    "type": {"const": "tool"}, **RECORDS["tool"],
}, open_to_more=True)
RECORD = {
    "type": "object", "description": "a record of a session's log: what an "
    "app reads of each kind is record.<type>",
    "properties": RECORD_FIELDS, "required": ["seq", "type"],
    "additionalProperties": True,
}
RECORD_ROWS = {"type": "array", "items": RECORD, "description": "the log's "
               "records"}

ADMITTED = {
    "admitted": {**BOOLEAN, "description": "false: this id was sent before "
                 "and its message is already in"},
    "session": SESSION,
    "handled_as": one_of("started", "steered", "queued", "reply"),
}
CHOICES = ("yes", "session", "saved", "no")
# What a session's next turn runs with
RUNS_WITH = shape("what a session's next turn runs with", {
    "mode": STRING, "model": STRING, "workspace": STRING,
    "thinking_effort": STRING,
    "judge_model": {**maybe(STRING), "description": "null: the session's "
                    "own model"},
    "judge_thinking_effort": STRING,
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
            changes=False):
    """One message: what it does, its fields (name → schema), which of them
    it must carry, its reply's fields (besides "ok"), for a message that
    stays open the events it streams, and whether it changes something:
    then it carries an `id`, which the server runs once."""
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
    }


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
        "A message to a session, or to a new one: a turn starts, or it "
        "steers or queues behind the running one, or it answers the open ask.",
        {
            "text": STRING, "session": maybe(SESSION),
            "mode": maybe(STRING), "model": maybe(STRING), "source": SOURCE,
            "thinking_effort": {**maybe(STRING), "description": "a new session's "
                                "thinking effort: off, low, high or max"},
            "reply_to": {**maybe(STRING), "description": "the open ask's id: "
                         "this text answers it"},
            "workspace": {**maybe(STRING), "description": "a new session's "
                          "folder"},
            "when_busy": one_of("steer", "queue"),
            "choice": {**STRING, "description": "one of the open ask's "
                       "`choices`, as picked at its box"},
            "rounds": {**BOOLEAN, "description": "the message is to be worked "
                       "on in rounds: ZERO is told, and its estimate goes to "
                       "the user"},
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
        "The sessions, each with its open ask; with `watch`, the list again "
        "at each change (archived ones left out).",
        {
            "archived": {**maybe(BOOLEAN), "description": "only the archived "
                         "(true), the others (false), or all (null)"},
            "workspace": {**maybe(STRING), "description": "only this folder's"},
            "watch": BOOLEAN,
        },
        reply={"sessions": rows("session", "one row a session")},
        stream={"sessions": rows("session", "the whole list"), "bye": STRING},
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
        {"session": SESSION, "after": INTEGER, "limit": maybe(INTEGER)},
        required=("session",),
        stream={"record": RECORD, "settled": SETTLED, "draft": OBJECT,
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
         "workspace": STRING, "thinking_effort": STRING,
         "judge_model": {**STRING, "description": "empty: the session's own "
                         "model"},
         "judge_thinking_effort": STRING},
        required=("session",),
        reply={"set": {"type": "object", "additionalProperties": maybe(STRING),
                       "description": "what was set, by name"},
               "next_turn": {**BOOLEAN, "description": "a running or waiting "
                             "turn holds the old values"}},
        changes=True,
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
            "session": shape("the session searched or read", {
                "id": SESSION, "title": STRING, "workspace": STRING,
                "state": STRING, "mode": STRING, "archived": BOOLEAN,
                "updated_at": NUMBER,
            }, open_to_more=True),
            "sessions": open_rows("the matching sessions, best first: a "
                                  "session's fields, record_count and its "
                                  "best record"),
            "records": open_rows("records: seq, kind (user, notice, reply, "
                                 "call, handoff), a call's tool or a notice's "
                                 "source, time, and a snippet (a match marked "
                                 "« ») or, near around, the whole text"),
            "session_count": INTEGER, "record_count": INTEGER,
            "matched_by": {**one_of("every word", "any word"),
                           "description": "what the records hold: every word, "
                           "or, when no record does, any of them"},
            "around": INTEGER, "earlier_count": INTEGER, "later_count": INTEGER,
        },
    ),
    "subagent/send": message(
        "A workflow's message: a new subagent of for_session, text its task, "
        "or text to one of its subagents (session). A subagent runs in its "
        "session's workspace, mode and model; its asks go where that "
        "session's other subagents' go, and its answer to whoever follows "
        "it. The one message besides those that only look that one of "
        "ZERO's own commands may send, and only for the session whose "
        "command it is.",
        {
            "text": STRING,
            "for_session": {**SESSION, "description": "the session the "
                            "workflow works for: its commands find its id in "
                            f"${SESSION_ID_ENV}"},
            "session": {**maybe(SESSION), "description": "one of for_session's "
                        "subagents, at any level; none: a new subagent"},
            "title": {**maybe(STRING), "description": "a new subagent's title; "
                      "none: its task's first words"},
            "source": SOURCE,
        },
        required=("text", "for_session", "source"),
        reply={"session": SESSION, "started": {**BOOLEAN, "description":
                                               "a new subagent was started"}},
        changes=True,
    ),
    "job/list": message(
        "A session's background jobs, ended ones too; with none, every "
        "running job.",
        {"session": SESSION},
        reply={"jobs": rows("job", "one row a job")},
    ),
    "job/stop": message(
        "Stop a background job.",
        {"job": STRING}, required=("job",),
        reply={"stopped": BOOLEAN},
        changes=True,
    ),
    "rounds/list": message(
        "A session's work in rounds, ended runs too; with none, every open "
        "one.",
        {"session": SESSION},
        reply={"rounds": rows("rounds", "one row a run")},
    ),
    "rounds/stop": message(
        "Stop work in rounds: no next round starts; a round under way ends its "
        "turn.",
        {"rounds": STRING}, required=("rounds",),
        reply={"stopped": BOOLEAN},
        changes=True,
    ),
    "grant/list": message(
        "The grants that hold in a session: what an ask's \"don't ask "
        "again\" allowed.",
        {"session": SESSION}, required=("session",),
        reply={"grants": open_rows("id, by, scope, grant, description")},
    ),
    "grant/forget": message(
        "Take a grant back.",
        {"session": SESSION, "grant": STRING},
        required=("session", "grant"),
        reply={"forgot": STRING},
        changes=True,
    ),
    "model/list": message(
        "Every server the server can reach, with its models by the names a "
        "session takes, or why it lists none.",
        reply={"servers": open_rows("server, title, models, and error where "
                                    "it listed none")},
    ),
    "model/features": message(
        "What a model honours of its API's features (thinking_off, "
        "thinking_on, strict_json, json_mode), as measured: each true or "
        "false, null where never measured. Measured now when none are kept "
        "or `test` is true, a few small calls to the model.",
        {"model": STRING, "test": BOOLEAN}, required=("model",),
        reply={"model": STRING, "api": STRING, "measured_at": maybe(STRING),
               "features": OBJECT},
        changes=True,
    ),
    "model/server/list": message(
        "Every model server the server knows, whether its key is there, the "
        "default model and its server, and why a session on it couldn't "
        "start (null: it can).",
        reply={"servers": open_rows("name, title, url, key_state, needs_key, "
                                    "user_added, in_catalog, context_window, "
                                    "models"),
               "default": maybe(STRING),
               "default_server": {**maybe(STRING), "description": "the server "
                                  "the default model goes to"},
               "not_ready_reason": maybe(STRING)},
    ),
    "model/server/probe": message(
        "A server's models, for a pick list. A typed url takes the typed key "
        "or none; a name alone, its own url and stored key.",
        {"name": STRING, "url": STRING, "key": STRING},
        reply={"url": STRING, "models": open_rows("id, context_window")},
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
        changes=True,
    ),
    "model/server/disconnect": message(
        "A server's key and the user's lines for it taken out.",
        {"name": STRING}, required=("name",),
        reply={"removed": STRING, "default_unset": BOOLEAN},
        changes=True,
    ),
    "mcp/list": message(
        "The MCP servers whose tools ZERO's sessions use, as the user listed "
        "them, each with its state (starting, ready, failed, gone, stopped), "
        "its error and how many tools it offers; and what the list says that "
        "ZERO can't use. Values of env and headers are left out.",
        reply={"servers": open_rows("name, transport, command, args, env, "
                                    "cwd, url, headers, tools, start_seconds, "
                                    "call_seconds, state, error, "
                                    "protocol_version"),
               "notes": STRINGS},
    ),
    "mcp/get": message(
        "One MCP server: its list row, its tools as the model calls them, what "
        "of its listing can't be offered, and its last words on stderr.",
        {"name": STRING}, required=("name",),
        reply={"server": {"type": "object", "additionalProperties": True}},
    ),
    "mcp/add": message(
        "An MCP server listed (a command ZERO starts, or a url), or copied "
        "from another agent's list (`from`: claude or codex; `tools` may "
        "narrow it); then started, the reply saying how its start went. One "
        "that fails stays listed.",
        {"name": STRING, "command": STRING,
         "args": STRINGS,
         "env": {"type": "object", "additionalProperties": STRING},
         "cwd": STRING, "url": STRING,
         "headers": {"type": "object", "additionalProperties": STRING},
         "tools": STRINGS,
         "start_seconds": NUMBER, "call_seconds": NUMBER,
         "from": one_of("claude", "codex")},
        required=("name",),
        reply={"server": {"type": "object", "additionalProperties": True},
               "notes": STRINGS},
        changes=True,
    ),
    "mcp/remove": message(
        "An MCP server taken off the list and stopped; its tools offered no "
        "more.",
        {"name": STRING}, required=("name",),
        reply={"removed": STRING},
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
