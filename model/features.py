"""What a model honours of its API's features, measured, never assumed.

Each API declares the features ZERO may ask through its standard fields
(apis/, FEATURES). Whether a model's server honours one is measured by a
small request whose effect shows: thinking off (no reasoning comes back),
thinking on (reasoning comes back), strict JSON (a reply that must fit a
schema a greeting never would), JSON mode (a JSON object comes back; a
model asked for JSON may give one anyway, so only a refusal or prose shows
it missing) and prompt progress (llama.cpp's progress chunks come back). A
server that refuses the field (400) doesn't honour it.

The results are kept per model in ZERO's state (paths.model_features_file),
with the API they were measured on: a server switched to another API is
measured again. A model never measured is taken to honour all of its API's
features, so ZERO sends the standard field: unmeasured is never patched
around. Prompt progress is no standard field but llama.cpp's own, so it goes
only to a model measured to honour it. Measured when a face asks for a model's features and none are kept,
and on its "Test" (model/service.py, `model/features`).
"""

import json
import time
from pathlib import Path

import openai

import paths
from core.llm import complete
from model import THINKING_EFFORT_FEATURES

# Each feature: what it is, in words, as the screens say it
FEATURES = {
    "thinking_off": "thinking off",
    "thinking_on": "thinking",
    "strict_json": "strict JSON",
    "json_mode": "JSON mode",
    "prefill_progress": "prompt progress",
}
# Output tokens a test may take: enough for a thinking model's short thought
OUTPUT_TOKENS = 1024
# A sum a model thinks about when it thinks at all
SUM = [{"role": "user", "content": "What is 23 times 17?"}]
# A greeting the schema below never fits unless the server holds the reply
# to it
GREETING = [{"role": "user", "content": "Say hello."}]
COLOURS = ("red", "green", "blue")
SCHEMA = {
    "name": "colour",
    "schema": {
        "type": "object",
        "properties": {"colour": {"type": "string", "enum": list(COLOURS)}},
        "required": ["colour"],
        "additionalProperties": False,
    },
}
# JSON mode's request names JSON, as some servers require
JSON_GREETING = [
    {"role": "system", "content": "Answer as a JSON object."},
    {"role": "user", "content": "Say hello."},
]


class FeatureError(Exception):
    """A model couldn't be measured (the server down, a wrong key): nothing
    is kept, so it is measured again next time."""


def for_thinking_effort(effort):
    """The feature a thinking effort needs; None for none sent."""
    return THINKING_EFFORT_FEATURES.get(effort)


def thinking_effort_sent(model, api, effort):
    """The effort to send: the one chosen, unless the model is measured not
    to honour it (then none: the chosen one is kept, not sent)."""
    if honoured(model, api, for_thinking_effort(effort)) is False:
        return None
    return effort


def prefill_progress_sent(model, api):
    """Whether to ask for prompt progress: only of a model measured to
    honour it."""
    return honoured(model, api, "prefill_progress") is True


def json_request(model, api, schema):
    """How to ask the model for JSON fitting `schema` ({"name", "schema"}):
    strict JSON unless measured not honoured, else JSON mode unless that
    isn't either, else nothing (the prompt asks for JSON itself)."""
    if honoured(model, api, "strict_json") is not False:
        return {"json_schema": schema}
    if honoured(model, api, "json_mode") is not False:
        return {"json_mode": True}
    return {}


# The file, read again whenever it changes
CACHE = {}
# The file's format: {"file_format": 1, "models": {model: record}}; before
# 0.7.1 the records sat at its top
FILE_FORMAT = 1


def records():
    """Every model's record: model → {"api", "measured_at", "features"}; a record
    kept before 0.6.2.8 says `measured`. A record this ZERO can't read is
    left out (measured again when asked)."""
    path = Path(paths.model_features_file())
    try:
        info = path.stat()
        stamp = (path, info.st_mtime_ns, info.st_size)
    except OSError:
        return {}
    if CACHE.get("stamp") != stamp:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if paths.FILE_FORMAT_KEY in data:
            data = data.get("models")
            data = data if isinstance(data, dict) else {}
        CACHE["records"] = {model: renamed(found)
                            for model, found in data.items() if readable(found)}
        CACHE["stamp"] = stamp
    return CACHE["records"]


def readable(found):
    """Whether a record has what this ZERO reads of one."""
    return (isinstance(found, dict) and isinstance(found.get("api"), str)
            and isinstance(found.get("features"), dict))


def renamed(found):
    """A record under today's names."""
    if isinstance(found, dict) and "measured" in found:
        found = {**found, "measured_at": found.get("measured_at", found["measured"])}
        del found["measured"]
    return found


def record(model, api):
    """The model's record measured on `api`, or None."""
    found = records().get(model)
    if not isinstance(found, dict) or found.get("api") != api.NAME:
        return None
    return found


def honoured(model, api, feature):
    """True or False as measured; None for a model never measured on its
    API, or no feature. A feature the API doesn't have is never honoured."""
    if feature is None:
        return None
    if feature not in api.FEATURES:
        return False
    found = record(model, api)
    if found is None:
        return None
    return found["features"].get(feature)


def json_object(text):
    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def measure(model, api, client, server_model, timeout):
    """Measure each of the API's features on the model, keep the record and
    return it. `server_model`: the model's name at its server. FeatureError
    when the server can't be asked."""

    def ask(messages, **standard):
        return complete(api, client, server_model, messages, silence=timeout,
                        max_output_tokens=OUTPUT_TOKENS, **standard)

    found = {}

    def test(feature, run):
        if feature not in api.FEATURES:
            found[feature] = False
            return
        try:
            found[feature] = run()
        except (openai.BadRequestError, openai.UnprocessableEntityError):
            # The server refuses the field
            found[feature] = False
        except Exception as e:
            raise FeatureError(
                f"couldn't measure {model}: {type(e).__name__}: {e}"
            ) from e

    test("thinking_off",
         lambda: not ask(SUM, effort="off").reasoning)
    test("thinking_on",
         lambda: bool(ask(SUM, effort="high").reasoning))
    # As the judge asks: at temperature 0, thinking off where it can be
    quiet = {"temperature": 0}
    if found["thinking_off"]:
        quiet["effort"] = "off"

    def fits():
        data = json_object(ask(GREETING, json_schema=SCHEMA, **quiet).content)
        return data is not None and set(data) == {"colour"} and (
            data["colour"] in COLOURS
        )

    test("strict_json", fits)
    test("json_mode", lambda: json_object(
        ask(JSON_GREETING, json_mode=True, **quiet).content) is not None)

    def progress():
        stream = api.open_stream(client, server_model, GREETING,
                                 prefill_progress=True,
                                 max_output_tokens=OUTPUT_TOKENS)
        try:
            return any(api.alive(event) for event in stream)
        finally:
            stream.close()

    test("prefill_progress", progress)
    row = {
        "api": api.NAME,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "features": found,
    }
    save_record(model, row)
    return row


def save_record(model, row):
    """The record added to the file, written whole; one writer at a time."""
    path = Path(paths.model_features_file())
    with paths.changing(path):
        CACHE.clear()
        kept = {**records(), model: row}
        data = {paths.FILE_FORMAT_KEY: FILE_FORMAT, "models": kept}
        paths.write_whole(path, json.dumps(data, indent=1, sort_keys=True) + "\n")
