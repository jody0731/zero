"""Every message a caller may send the host: the one door into ZERO.

Every face goes through it: the full screen, the plain commands (each mirrors
one message), other agents (through `zero mcp serve`) and apps to come, in any
language. A client sends one JSON object on one line and gets one line back:

    {"message": "host/hello", "protocol": 2}
        → {"ok": true, "protocol": 2, "build": "<hash>", "pid": 1234, ...}
    {"message": "session/send", "id": "<uuid>", "text": "...",
     "source": "cli", "workspace": "/a/folder", "when_busy": "steer"}
        → {"ok": true, "admitted": true, "session": "<id>",
           "disposition": "started" | "steered" | "queued" | "reply"}
    anything wrong → {"ok": false, "error": "..."}

Messages are named noun/verb, as a tree (`session/stop`, `job/stop`,
`model/provider/connect`); the commands have the same names
(`zero session stop ID`). Each message is whole in one request: it carries
all it needs, and the host keeps nothing between two of them. MESSAGES below
holds each one's fields and reply as JSON Schema; protocol/schema.json, which
ships, is written from it (protocol/schema.py), so an app in any language is
built against that file.

Two messages stay open and stream events, one JSON object a line:
`session/watch` (the records after a seq, then each new one; among them
`withdrawn`, what a stop gave back; `settled`, nothing more happens on its
own, also sent after the catch-up when it holds already; and `draft`, the
model's reply as it streams in, whole so far, ten a second at most, never
logged), and `session/list` with `watch` (the list, then the whole list again
each time it changes). Each ends with a `bye` and its reason when the host
closes it ("stopped": stay down until the user types; "restart": a newer
build, reconnect at once).

A component's messages (job/…, grant/…, model/…) are described here and
handled in the component's own host part, as a tool is described in core's
registry and runs in its own folder; so a client imports this, and never a
component. Which messages only look, so that ZERO's own commands may send
them, is said by whoever serves them (host/server.py, a part's `looking`).
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


SESSION = {**STRING, "description": "a session's id"}
# A message id the sender picks; the host admits each id once, so a retry
# after a dropped connection is safe
MESSAGE_ID = {**STRING, "minLength": 1, "maxLength": 200}
SOURCE = {**STRING, "description": "who sends it: a client's own name"}
ADMITTED = {
    "admitted": BOOLEAN,
    "session": SESSION,
    "disposition": one_of("started", "steered", "queued", "reply"),
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
    "host/hello": message(
        "The host's versions and what keeps it busy; a client's first look.",
        {"protocol": {**INTEGER, "description": "the client's protocol; the "
                      "host refuses another"}},
        reply={
            "protocol": INTEGER, "build": STRING, "pid": INTEGER,
            "started": NUMBER, "busy": OBJECT, "last": maybe(SESSION),
            "mode": STRING, "model": maybe(STRING),
            "modes": {"type": "array", "items": STRING},
            "default_model": maybe(STRING),
        },
    ),
    "host/stop": message(
        "Stop the host; while anything runs it says what, unless forced.",
        {"force": BOOLEAN, "reason": one_of("stopped", "restart")},
        reply={"stopping": BOOLEAN, "busy": OBJECT},
    ),
    "session/send": message(
        "A message to a session, or to a new one: a turn starts, or it "
        "steers or queues behind the running one, or it answers the open ask.",
        {
            "id": MESSAGE_ID, "text": STRING, "session": maybe(SESSION),
            "mode": maybe(STRING), "model": maybe(STRING), "source": SOURCE,
            "effort": {**maybe(STRING), "description": "a new session's "
                       "thinking effort: off, low, high or max"},
            "reply_to": {**maybe(STRING), "description": "the open ask's id: "
                         "this text answers it"},
            "workspace": {**maybe(STRING), "description": "a new session's "
                          "folder"},
            "when_busy": one_of("steer", "queue"),
            "choice": {**one_of(*CHOICES), "description": "one of the open "
                       "ask's choices, as picked at its box"},
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
        reply={"records": rows("the log's records"), "gap": INTEGER,
               "runs_with": {**OBJECT, "description": "what its next turn runs "
                             "with: mode, model, workspace, effort, "
                             "judge_model (null: the session's model) and "
                             "judge_effort"}},
    ),
    "session/watch": message(
        "A session's records after a seq, then each new one as it comes.",
        {"session": SESSION, "after": INTEGER, "limit": maybe(INTEGER)},
        required=("session",),
        stream={"record": OBJECT, "settled": OBJECT, "draft": OBJECT,
                "withdrawn": OBJECT, "gap": INTEGER, "bye": STRING},
    ),
    "session/seen": message(
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
    "session/set": message(
        "Change a session's mode, model, workspace, effort or judge from its "
        "next turn.",
        {"session": SESSION, "mode": STRING, "model": STRING,
         "workspace": STRING, "effort": STRING,
         "judge_model": {**STRING, "description": "empty: the session's own "
                         "model"},
         "judge_effort": STRING},
        required=("session",),
        reply={"set": OBJECT, "next_turn": BOOLEAN},
    ),
    "job/list": message(
        "A session's background jobs, ended ones too; with none, every "
        "running job.",
        {"session": SESSION},
        reply={"jobs": rows("one row a job, its state in words in `said`")},
    ),
    "job/stop": message(
        "Stop a background job.",
        {"job": STRING}, required=("job",),
        reply={"stopped": BOOLEAN},
    ),
    "grant/list": message(
        "The grants that hold in a session: what an ask's \"don't ask "
        "again\" allowed.",
        {"session": SESSION}, required=("session",),
        reply={"grants": rows("id, by, scope, grant, what")},
    ),
    "grant/forget": message(
        "Take a grant back.",
        {"session": SESSION, "grant": STRING},
        required=("session", "grant"),
        reply={"forgot": STRING},
    ),
    "model/list": message(
        "Every server the host can reach, with its models by the names a "
        "session takes, or why it lists none.",
        reply={"servers": rows("one row a server")},
    ),
    "model/features": message(
        "What a model honours of its API's features (thinking_off, "
        "thinking_on, strict_json, json_mode), as measured: each true or "
        "false, null where never measured. Measured now when none are kept "
        "or `test` is true, a few small calls to the model.",
        {"model": STRING, "test": BOOLEAN}, required=("model",),
        reply={"model": STRING, "api": STRING, "measured": STRING,
               "features": OBJECT},
    ),
    "model/provider/list": message(
        "Every model server the host knows, whether its key is there, the "
        "default model, and why a session on it couldn't start (null: it can).",
        reply={"providers": rows("name, title, url, key, needs_key, own, "
                                 "shipped, context, models"),
               "default": maybe(STRING), "ready": maybe(STRING)},
    ),
    "model/provider/probe": message(
        "A server's models, for a pick list. A typed url takes the typed key "
        "or none; a name alone, its own url and stored key.",
        {"name": STRING, "url": STRING, "key": STRING},
        reply={"url": STRING, "models": rows("id, context")},
    ),
    "model/provider/connect": message(
        "A server's model tested with a call of a few tokens, then saved: "
        "the key in keys.toml, the server in models.toml; the first model "
        "connected "
        "becomes the default.",
        {"name": STRING, "url": STRING, "key": STRING, "model": STRING,
         "context": {**INTEGER, "minimum": 1}, "title": STRING,
         "default": BOOLEAN},
        required=("name", "model"),
        reply={"model": STRING, "default": BOOLEAN},
    ),
    "model/provider/disconnect": message(
        "A server's key and the user's lines for it taken out.",
        {"name": STRING}, required=("name",),
        reply={"removed": STRING, "default_unset": BOOLEAN},
    ),
    "setting/list": message(
        "ZERO's settings, each with its value, and what is wrong with "
        "config.toml (null: nothing).",
        reply={"settings": rows("name, group, label, default, kind, choices, "
                                "least, off, shown, needs, runs_on, value"),
               "error": maybe(STRING)},
    ),
    "setting/set": message(
        "Write one of ZERO's settings to config.toml (a face's own table, "
        "the full screen's [cli], is the face's).",
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
