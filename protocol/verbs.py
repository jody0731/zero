"""Every verb a caller may send the host, with the fields it takes.

A client sends one JSON object on one line and gets one line back:

    {"verb": "hello", "protocol": 1}
        → {"ok": true, "protocol": 1, "build": "<hash>", "pid": 1234,
           "started": <time>, "busy": {...}}
    {"verb": "send", "id": "<uuid>", "text": "...", "session": null,
     "workspace": "/a/folder", "when_busy": "steer"}
        → {"ok": true, "admitted": true, "session": "<id>",
           "disposition": "started" | "steered" | "queued" | "reply"}
    (a line typed at an ask's prompt also carries "reply_to": "<the ask's id>",
    and one picked at its box "choice": "yes" | "session" | "saved" | "no";
    "when_busy": "queue" makes a follow-up, after the running turn's answer)
    {"verb": "continue", "id": "<uuid>", "session": "<id>"}  → as send's
    {"verb": "list", "archived": false, "workspace": null}
        → {"ok": true, "sessions": [{..., "ask": {...} or null, "unseen": ...}]}
    {"verb": "history", "session": "<id>", "after": 0, "limit": 50,
     "before": null}  → {"ok": true, "records": [...], "gap": <before them>}
    {"verb": "seen", "session": "<id>", "seq": 12}  → {"ok": true}
    {"verb": "rename", "session": "<id>", "title": "..."}  → {"ok": true}
    {"verb": "archive", "session": "<id>", "archived": true}  → {"ok": true}
    {"verb": "delete", "session": "<id>"}  → {"ok": true, "trash": "<where>"}
    {"verb": "jobs"}  → {"ok": true, "jobs": [{"id": ..., "session": ..., ...}]}
    (with "session": that session's jobs, ended ones too)
    {"verb": "stop_job", "job": "<id>"}  → {"ok": true, "stopped": true}
    {"verb": "permissions", "session": "<id>"}
        → {"ok": true, "grants": [{"id", "by", "scope", "grant", "what"}, ...]}
    {"verb": "forget", "session": "<id>", "grant": "<id>"}
        → {"ok": true, "forgot": "<id>"}
    {"verb": "settings"}
        → {"ok": true, "settings": [{"name", "group", "label", "default", "kind",
           "choices", "least", "off", "shown", "value"}, ...],
           "error": what is wrong with config.toml, or null}
    {"verb": "set_setting", "name": "max_steps", "value": 80}
        → {"ok": true, "set": {"max_steps": 80}}
    (ZERO's settings in config.toml, which the host writes; a face's own
    table, the CLI's [cli], is the face's)
    {"verb": "shutdown", "force": false}  → {"ok": true, "stopping": true}, or
        {"ok": true, "stopping": false, "busy": {...}} while anything runs
    anything wrong    → {"ok": false, "error": "..."}

`watch` stays open: the records after a seq, then each new one as the log
appends it, and a `bye` with its reason when the host closes it ("stopped": stay
down until the user types; "restart": a newer build, reconnect at once). Among
the records come `withdrawn` (what a stop gave back), `settled` (nothing
more happens on its own; also sent after the catch-up when it holds already)
and `draft` (the model's reply as it streams in, whole so far, ten a second at
most; the next record replaces it, and it is never logged).
`watch_list` stays open too: the session list (archived ones left out), then
the whole list again each time it changes.

A component's verbs (jobs, stop_job, permissions, forget, models) are described here
and handled in the component's own host part, as a tool is described in core's
registry and runs in its own folder; so a client imports this, and never a
component. Which verbs only look, so that ZERO's own commands may send them, is
said by whoever serves them (host/server.py, a part's `looking`). Events stay
documented shapes.
"""

# Each verb and the fields a request may carry besides "verb", in the order
# the host lists them
VERBS = {
    "hello": ("protocol",),
    "send": ("id", "text", "session", "mode", "model", "source", "reply_to",
             "workspace", "when_busy", "choice"),
    "stop": ("id", "session", "source"),
    "continue": ("id", "session", "source"),
    "list": ("archived", "workspace"),
    "history": ("session", "after", "limit", "before"),
    "watch": ("session", "after", "limit"),
    "watch_list": (),
    "seen": ("session", "seq"),
    "rename": ("session", "title"),
    "archive": ("session", "archived"),
    "delete": ("session",),
    "set": ("session", "mode", "model", "workspace", "effort"),
    "jobs": ("session",),
    "stop_job": ("job",),
    "permissions": ("session",),
    "forget": ("session", "grant"),
    "models": (),
    "settings": (),
    "set_setting": ("name", "value"),
    "shutdown": ("force", "reason"),
}


def request(verb, **fields):
    """A request as a client sends it; ValueError for a verb or a field the
    protocol doesn't have."""
    if verb not in VERBS:
        raise ValueError(f"no verb {verb!r} in ZERO's protocol")
    unknown = [name for name in fields if name not in VERBS[verb]]
    if unknown:
        raise ValueError(f"{verb} takes no {', '.join(unknown)}")
    return {"verb": verb, **fields}
