"""Resolve the selected route's effort and optional request features.

Catalog declarations and exact model-directory metadata are passive. An
explicit `model features` command can measure a route with bounded calls.
Those measurements are tool-free, belong to the model, URL and API, and
cannot certify an entire range of thinking efforts.
"""

import json
import time
from pathlib import Path

import openai

import paths
from core.llm import UnsupportedModelRequest, complete, request_error_text
from model import MODEL_DEFAULT_EFFORT, THINKING_EFFORTS

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


def effort_choices(model, server, *, tools=False):
    """Availability of ZERO's choices on this route. None means unverified."""
    return {choice: effort_support(model, server, choice, tools)
            for choice in THINKING_EFFORTS}


def effort_support(model, server, choice, tools):
    if tools and server.tool_efforts is not None and choice not in server.tool_efforts:
        return False
    if choice == MODEL_DEFAULT_EFFORT:
        return True
    if server.reasoning_efforts is not None:
        return choice in server.reasoning_efforts
    if (reported := server.reported_reasoning()) is not None:
        if choice == "off" and reported.get("mandatory") is True:
            return False
        if "supported_efforts" not in reported:
            return False
        levels = reported["supported_efforts"]
        if levels is None:
            return True
        if isinstance(levels, list):
            wire = "none" if choice == "off" else choice
            return wire in levels or (choice == "max" and "xhigh" in levels)
    found = record(model, server.api, route=server.history_route) or {}
    measured = found.get("efforts", {}).get(choice)
    # A tool-free measurement cannot vouch for a tool-equipped request.
    return None if tools and measured is True else measured


def request_effort(model, server, effort, *, tools=False):
    """Translate a compatible effort. An explicit unknown choice can be tried."""
    choice = effort or MODEL_DEFAULT_EFFORT
    if effort_support(model, server, choice, tools) is False:
        if tools and server.tool_efforts == []:
            raise UnsupportedModelRequest(tool_error(model, server))
        allowed = effort_choices(model, server, tools=tools)
        choices = ", ".join(
            "Model default" if value == MODEL_DEFAULT_EFFORT else value
            for value, supported in allowed.items() if supported is not False
        )
        raise UnsupportedModelRequest(
            f"{model} cannot use thinking effort {choice} with {server.api.TITLE}"
            f"{' and ZERO’s tools' if tools else ''}. Choose {choices}."
        )
    if choice == MODEL_DEFAULT_EFFORT:
        return None
    if choice == "max" and server.max_reasoning_effort:
        return server.max_reasoning_effort
    if choice == "max" and server.reasoning_efforts is None:
        reported = server.reported_reasoning() or {}
        levels = reported.get("supported_efforts")
        if isinstance(levels, list) and "xhigh" in levels and "max" not in levels:
            return "xhigh"
    return choice


def tool_error(model, server):
    """Why a normal turn cannot use this route, even with model-default effort."""
    if server.tool_efforts == []:
        return (f"{model} cannot use ZERO’s tools with {server.api.TITLE}. "
                "Choose a different model or API.")
    return None


def prefill_progress_sent(model, api, *, route=None):
    """Whether to ask for prompt progress: only of a model measured to
    honour it."""
    return honoured(model, api, "prefill_progress", route=route) is True


def json_request(model, api, schema, *, unmeasured=True, route=None):
    """How to ask the model for JSON fitting `schema` ({"name", "schema"}):
    strict JSON unless measured not honoured, else JSON mode unless that
    isn't either, else nothing (the prompt asks for JSON itself).
    With unmeasured=False only measured support enables a native format."""
    strict = honoured(model, api, "strict_json", route=route)
    if strict is True or (strict is None and unmeasured):
        return {"json_schema": schema}
    mode = honoured(model, api, "json_mode", route=route)
    if mode is True or (mode is None and unmeasured):
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


def record(model, api, *, route=None):
    """The model's record measured on `api`, or None."""
    found = records().get(model)
    if not isinstance(found, dict) or found.get("api") != api.NAME:
        return None
    if route is not None and found.get("route") != route:
        return None
    return found


def honoured(model, api, feature, *, route=None):
    """True or False as measured; None for a model never measured on its
    API, or no feature. A feature the API doesn't have is never honoured."""
    if feature is None:
        return None
    if feature not in api.FEATURES:
        return False
    found = record(model, api, route=route)
    if found is None:
        return None
    return found["features"].get(feature)


def json_object(text):
    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def measure(model, api, client, server_model, timeout, *, server=None):
    """Measure each of the API's features on the model, keep the record and
    return it. `server_model`: the model's name at its server. FeatureError
    when the server can't be asked."""

    def ask(messages, **standard):
        return complete(api, client, server_model, messages, silence=timeout,
                        max_output_tokens=OUTPUT_TOKENS, **standard)

    found = {}
    efforts = {}

    def test(feature, run, *, choice=None):
        if feature not in api.FEATURES:
            found[feature] = False
            return
        try:
            found[feature] = run()
            if choice is not None and found[feature]:
                efforts[choice] = True
        except (openai.BadRequestError, openai.UnprocessableEntityError):
            # The server refuses the field
            found[feature] = False
            if choice is not None:
                efforts[choice] = False
        except Exception as e:
            raise FeatureError(
                f"couldn't measure {model}: {request_error_text(e, client)}"
            ) from None

    if server is not None and server.reasoning_efforts is not None:
        found["thinking_off"] = "off" in server.reasoning_efforts
        found["thinking_on"] = any(c != "off" for c in server.reasoning_efforts)
    else:
        test("thinking_off", lambda: not ask(SUM, effort="off").reasoning,
             choice="off")
        test("thinking_on", lambda: bool(ask(SUM, effort="high").reasoning),
             choice="high")
    # As the judge asks: at temperature 0, thinking off where it can be
    quiet = {}
    if server is None or server.accepts_zero_temperature:
        quiet["temperature"] = 0
    if found["thinking_off"]:
        quiet["effort"] = "off"
        if server is not None and server.chat_thinking_field:
            quiet["thinking_field"] = server.chat_thinking_field

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
    if server is not None:
        row["route"] = server.history_route
        row["efforts"] = efforts
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
