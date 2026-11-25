"""The protocol as JSON Schema, written from messages.py into schema.json.

schema.json ships with ZERO, so an app in any language is built against it:
each message's request and reply are in `$defs` (`session.send` and
`session.send.reply`, the name's slashes as dots), the requests together
under `oneOf`, the error reply every message may get, the rows replies carry
(`row.session`, `row.ask`, …), the native `Entry` envelope (`entry`,
`entry.timeline`) and the wire `record` shape. A reply may grow fields within
a protocol, so schema.json lets an object carry more than it names; the tests
hold every reply the server sends to the strict form (`strict`), where a row
or record carries exactly its fields. A test holds the file to messages.py;
after a change to a message:

    python -m protocol.schema
"""

import copy
import json
from pathlib import Path

from protocol import version
from protocol.messages import (
    DEFS, ENTRY, EVERY_REQUEST, MESSAGES, MODEL_FAILURE_REF, RECORD, REPLAYED,
    ROWS, TIMELINE_ENTRY, UNKNOWN_FIELDS,
)

PATH = Path(__file__).with_name("schema.json")


def key(name):
    return name.replace("/", ".")


def request_schema(name, spec):
    properties = {"message": {"const": name}, **EVERY_REQUEST, **spec["fields"]}
    return {
        "type": "object",
        "description": spec["about"],
        "properties": properties,
        "required": ["message", *spec["required"]],
        "additionalProperties": False,
        "x-changes": spec["changes"],
    }


def reply_schema(spec):
    return {
        "type": "object",
        "properties": {"ok": {"const": True}, **spec["reply"]},
        "required": ["ok"],
    }


def stream_schema(spec):
    stream = spec["stream"]
    if "oneOf" in stream:
        return stream
    return {
        "type": "object",
        "description": "one event a line, until a bye",
        "properties": stream,
    }


def build():
    defs = {"error": {
        "type": "object",
        "properties": {"ok": {"const": False}, "error": {"type": "string"},
                       "replayed": REPLAYED, "unknown_fields": UNKNOWN_FIELDS,
                       "model_failure": MODEL_FAILURE_REF,
                       "over_limit": {"type": "boolean"}},
        "required": ["ok", "error"],
    }}
    defs["entry"] = ENTRY
    defs["entry.timeline"] = TIMELINE_ENTRY
    for name, definition in DEFS.items():
        defs[name] = definition
    for name, spec in MESSAGES.items():
        defs[key(name)] = request_schema(name, spec)
        defs[key(name) + ".reply"] = reply_schema(spec)
        if spec["stream"]:
            defs[key(name) + ".event"] = stream_schema(spec)
    for name, row in ROWS.items():
        defs[f"row.{name}"] = row
    defs["record"] = RECORD
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"zero-protocol-{version.PROTOCOL}",
        "title": f"Z.E.R.O. protocol {version.PROTOCOL}",
        "description": (
            "One JSON object a line over the server's Unix socket "
            "(~/.zero/run/zero.sock; `zero server start --json` names it, "
            "starting the server when none answers): a request, then its "
            "reply; session/watch and session/list with watch stream events. "
            "Each request names its message in `message` and its protocol in "
            "`protocol`; a message marked x-changes carries an `id` the "
            "sender picks, run once. Where the socket can't tell who "
            "connects, `token` carries the token file's text."
        ),
        "x-protocol": version.PROTOCOL,
        "x-oldest-protocol": version.OLDEST,
        "oneOf": [{"$ref": f"#/$defs/{key(name)}"} for name in MESSAGES],
        "$defs": defs,
    }


def strict(schema):
    """The schema with every object that names its fields and says nothing
    of others closed to them: what the server itself promises to send."""
    schema = copy.deepcopy(schema)

    def close(node):
        if isinstance(node, dict):
            if ("properties" in node and node.get("type") == "object"
                    and "additionalProperties" not in node):
                node["additionalProperties"] = False
            for value in node.values():
                close(value)
        elif isinstance(node, list):
            for value in node:
                close(value)

    close(schema)
    return schema


def text():
    return json.dumps(build(), indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    PATH.write_text(text())
    print(f"Wrote {PATH}.")
