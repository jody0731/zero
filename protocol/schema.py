"""The protocol as JSON Schema, written from messages.py into schema.json.

schema.json ships with ZERO, so an app in any language is built against it:
each message's request and reply are in `$defs` (`session.send` and
`session.send.reply`, the name's slashes as dots), the requests together
under `oneOf`, and the error reply every message may get. A test holds the
file to messages.py; after a change to a message:

    python -m protocol.schema
"""

import json
from pathlib import Path

from protocol import version
from protocol.messages import MESSAGES

PATH = Path(__file__).with_name("schema.json")


def key(name):
    return name.replace("/", ".")


def request_schema(name, spec):
    properties = {"message": {"const": name}, **spec["fields"]}
    return {
        "type": "object",
        "description": spec["about"],
        "properties": properties,
        "required": ["message", *spec["required"]],
        "additionalProperties": False,
    }


def reply_schema(spec):
    # A reply may grow fields within a protocol, so others are allowed
    return {
        "type": "object",
        "properties": {"ok": {"const": True}, **spec["reply"]},
        "required": ["ok"],
    }


def stream_schema(spec):
    return {
        "type": "object",
        "description": "one event a line, until a bye",
        "properties": spec["stream"],
    }


def build():
    defs = {"error": {
        "type": "object",
        "properties": {"ok": {"const": False}, "error": {"type": "string"}},
        "required": ["ok", "error"],
    }}
    for name, spec in MESSAGES.items():
        defs[key(name)] = request_schema(name, spec)
        defs[key(name) + ".reply"] = reply_schema(spec)
        if spec["stream"]:
            defs[key(name) + ".event"] = stream_schema(spec)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"zero-protocol-{version.PROTOCOL}",
        "title": f"Z.E.R.O. protocol {version.PROTOCOL}",
        "description": (
            "One JSON object a line over the server's Unix socket "
            "(~/.zero/run/zero.sock): a request, then its reply; "
            "session/watch and session/list with watch stream events. Each "
            "request names its message in `message`."
        ),
        "x-protocol": version.PROTOCOL,
        "oneOf": [{"$ref": f"#/$defs/{key(name)}"} for name in MESSAGES],
        "$defs": defs,
    }


def text():
    return json.dumps(build(), indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    PATH.write_text(text())
    print(f"Wrote {PATH}.")
