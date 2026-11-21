"""Which model, at which server, with which key.

The servers come from two files: ZERO's catalog (catalog.toml beside this
file: the ones it ships with) and the user's models.toml (paths.py: the
servers they added, and any line of a shipped one they changed, by naming
the same server). A model name picks its server by its prefix
("myserver/some-model"); a name without a known prefix goes to the catalog's
server for bare model names. A model's limits come from its endpoint and
the published catalog. Saved windows can narrow those limits or supply a
missing window. A server's `api` pins the API it speaks (apis/). Otherwise
ZERO prefers Responses and uses Chat Completions for documented exceptions.
A model's price is its own line, else its server's, else the
one a release filled in from models.dev's list (prices.toml beside this
file): no user types a price, and a model with none anywhere has none.

An API key comes from the server's environment variable or from the keys
file (paths.py), straight into the client; the harness never puts it in the
environment its commands inherit (key_envs: what the sandbox strips).
"""

import json
import os
import sys
import tomllib
import urllib.error
import urllib.request
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from openai import OpenAI

import paths
from core.llm import complete, provider_history, remember_provider_reply
from model import THINKING_EFFORTS, directory, features, limit_catalog
from model.apis import APIS, DEFAULT, SEARCH_APIS
from model.limits import TokenLimits, endpoint, model_id, smaller, token_count
from settings import stamp

CATALOG = Path(__file__).with_name("catalog.toml")
# Prices a release filled in: {server: {model: [cache hit, miss, output]}}
PRICES = Path(__file__).with_name("prices.toml")
# A server's lines, and the ones a model may have of its own. context_stop
# set where the context guard stopped a turn until 0.6.3; read and left
# alone since, as the line is compact_at_tokens (context/)
SERVER_FIELDS = {
    "title": str, "url": str, "key_env": str, "api": str,
    "context_window": int, "context_stop": int, "price": list, "models": dict,
    "refuses_overlong_requests": bool,
    "search_api": str, "search_model": str, "bare_model_names": bool,
    "max_reasoning_effort": str, "context_windows": dict,
    "max_input_tokens": int, "max_output_tokens": int,
    "models_dev_provider": str,
    "reply_api": str, "responses_url": str, "chat_thinking_field": str,
    "responses_since_version": list,
    "reasoning_efforts": list, "chat_reasoning_efforts": list,
    "responses_reasoning_efforts": list,
    "chat_tool_efforts": list, "accepts_zero_temperature": bool,
    "replays_encrypted_reasoning": bool, "replays_response_items": bool,
    "reports_reasoning_efforts": bool,
    "suggested_models": list,
}
MODEL_FIELDS = ("context_window", "max_input_tokens", "max_output_tokens",
                "context_stop", "price", "max_reasoning_effort", "reply_api",
                "reasoning_efforts", "chat_reasoning_efforts", "chat_tool_efforts",
                "responses_reasoning_efforts",
                "accepts_zero_temperature", "chat_thinking_field")
LIMIT_FIELDS = ("context_window", "max_input_tokens", "max_output_tokens")
REPLY_FIELDS = (
    "reply_api", "responses_url", "responses_since_version",
    "reasoning_efforts", "chat_reasoning_efforts",
    "responses_reasoning_efforts",
    "chat_tool_efforts", "max_reasoning_effort", "accepts_zero_temperature",
    "replays_encrypted_reasoning", "replays_response_items", "chat_thinking_field",
    "reports_reasoning_efforts",
    "suggested_models",
)
ENDPOINT_FIELDS = ("key_env", "search_api", "search_model",
                   "refuses_overlong_requests", *LIMIT_FIELDS, *REPLY_FIELDS)
# Lines named otherwise before 0.6.2.8, read under their names now
RENAMED = {"name": "title", "context": "context_window"}
# Lines a server had before 0.6.2.7: what a model honours is measured now;
# read and left out
RETIRED = ("json_format", "thinking")
# The formats of models.toml and keys.toml, a line at each one's top
# (paths.FILE_FORMAT_KEY)
MODELS_FILE_FORMAT = 1
KEYS_FILE_FORMAT = 1
# What a keyless server is sent for a key: the client needs one
NO_KEY = "none"
# Servers whose root answers no /tokenize, by URL
NO_COUNT = set()
# Seconds a token count may take; past it, counted by bytes this time
COUNT_SECONDS = 10
# A server's answer with no count in it
NONE = object()
# Counts kept by the text's hash, so a measure asks once for each text
COUNTS_KEPT = 512


# The catalog and the filled prices, read once; the user's file again
# whenever it changes
CACHE = {}


class ModelServerError(Exception):
    """A model can't be reached as configured (no key, say): stop at startup."""


def reply_url(base, responses, api):
    if api.NAME != "responses" or responses is None:
        return base
    source, target = urlsplit(base), urlsplit(responses)
    if (source.scheme, source.netloc) != (target.scheme, target.netloc):
        raise ModelServerError("responses_url must use the API URL's origin")
    return responses


def reply_api(name, entry, model, line):
    configured = entry.get("api") or line.get("reply_api") or entry.get("reply_api")
    if configured:
        return APIS[configured]
    minimum = entry.get("responses_since_version")
    if model and minimum and canonical_endpoint(name, entry["url"]):
        version = directory.server_version(entry["url"])
        if version is not None and version < tuple(minimum):
            return APIS["chat_completions"]
    return APIS[DEFAULT]


class ModelServer:
    """A server as one of its models sees it: the server's lines with the
    model's own over them."""

    def __init__(self, name, entry, model=None, *, refresh=True):
        line = entry.get("models", {}).get(model, {}) if model else {}
        merged = {**entry, **line}
        self.name = name
        # What the screens show
        self.title = entry.get("title", name)
        self.url = entry["url"]
        self._entry = entry
        self._model = model
        self._reported = directory.reported(self.url, model) if model else TokenLimits()
        self.set_limits(self._reported, refresh=refresh)
        self.api = reply_api(name, entry, model, line)
        self.reply_url = reply_url(self.url, entry.get("responses_url"), self.api)
        route = (name, endpoint(self.reply_url), self.api.NAME, model)
        self.history_route = sha256(json.dumps(route).encode()).hexdigest()
        self.reasoning_efforts = merged.get("reasoning_efforts")
        self.tool_efforts = None
        self.chat_thinking_field = None
        if self.api.NAME == "chat_completions":
            self.reasoning_efforts = merged.get(
                "chat_reasoning_efforts", self.reasoning_efforts,
            )
            self.tool_efforts = merged.get("chat_tool_efforts")
            self.chat_thinking_field = merged.get("chat_thinking_field")
        else:
            self.reasoning_efforts = merged.get(
                "responses_reasoning_efforts", self.reasoning_efforts,
            )
        self.max_reasoning_effort = merged.get("max_reasoning_effort")
        if (self.api.NAME == "chat_completions"
                and "responses_reasoning_efforts" in line):
            self.max_reasoning_effort = None
        self.accepts_zero_temperature = merged.get("accepts_zero_temperature", True)
        self.replays_encrypted_reasoning = entry.get(
            "replays_encrypted_reasoning", False,
        )
        self.replays_response_items = entry.get("replays_response_items", True)
        self.reports_reasoning_efforts = entry.get("reports_reasoning_efforts", False)
        # None: no key needed
        self.key_env = entry.get("key_env")
        # Whether it refuses a request longer than its context, where a
        # server may cut one silently: the context's estimate may then
        # learn from its counts
        # (context/room.py). Not known, not assumed
        self.refuses_overlong_requests = entry.get(
            "refuses_overlong_requests", False)
        # USD per 1M tokens at peak price: (cache hit, cache miss, output)
        price = merged.get("price")
        if not price and model:
            price = filled_prices().get(name, {}).get(model)
        self.price = tuple(price) if price else None

    def set_limits(self, reported, *, refresh=False):
        entry, model = self._entry, self._model
        self._reported = reported
        self.verified_limits, limits = (
            model_limits(self.name, entry, model, refresh=refresh, reported=reported)
            if model is not None else (TokenLimits(), TokenLimits.read(entry))
        )
        self.context_window = limits.context_window
        self.max_input_tokens = limits.max_input_tokens
        self.max_output_tokens = limits.max_output_tokens
        # Legacy windows alone were compaction targets. Verified or scoped
        # windows bind requests, including any smaller saved cap.
        bounded = self.verified_limits.context_window or saved_window(entry, model)
        self.request_context_window = limits.context_window if bounded else None

    def refresh_limits(self):
        """Apply a new endpoint observation to an already open session."""
        reported = directory.reported(self.url, self._model)
        if reported != self._reported:
            self.set_limits(reported)

    def reported_reasoning(self):
        if self.reports_reasoning_efforts:
            return directory.reported_reasoning(self.url, self._model)
        return None


def filled_prices():
    """The prices a release filled in, read once; none without the file."""
    if "prices" not in CACHE:
        try:
            CACHE["prices"] = tomllib.loads(PRICES.read_text(encoding="utf-8"))
        except FileNotFoundError:
            CACHE["prices"] = {}
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ModelServerError(f"can't read {PRICES}: {e}") from e
    return CACHE["prices"]


def load(path, where, notes=None):
    """A servers file's tables, checked; no file, none. What this ZERO
    doesn't know of it (a newer format, a line it has no such name for, a
    line gone since an older release) is left out, and said in `notes`, a
    list, when one is given; a line it knows with a value no server can use
    stops it."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ModelServerError(f"can't read {where}: {e}") from e
    told = notes if notes is not None else []
    found = data.pop(paths.FILE_FORMAT_KEY, MODELS_FILE_FORMAT)
    if isinstance(found, int) and found > MODELS_FILE_FORMAT:
        told.append(paths.newer_note(where, found, MODELS_FILE_FORMAT))
    loose = [name for name, entry in data.items() if not isinstance(entry, dict)]
    if loose:
        told.append(paths.unknown_note(where, loose))
    data = {name: known_lines(name, renamed(entry), where, told)
            for name, entry in data.items() if name not in loose}
    for name, entry in data.items():
        problem = check(name, entry)
        if problem:
            raise ModelServerError(f"{where}: {problem}")
    return data


def known_lines(name, entry, where, notes):
    """A server's table less the lines this ZERO doesn't know, each said."""
    unknown = [field for field in entry if field not in SERVER_FIELDS]
    gone = [field for field in unknown if field in RETIRED]
    if gone:
        notes.append(
            f"{where} [{name}]: {', '.join(gone)} gone since 0.6.2.7 (what a "
            "model honours is measured now: zero model features MODEL), left "
            "as it is")
    if set(unknown) - set(gone):
        notes.append(paths.unknown_note(f"{where} [{name}]",
                                        set(unknown) - set(gone)))
    entry = {field: value for field, value in entry.items()
             if field not in unknown}
    models = entry.get("models")
    if isinstance(models, dict):
        lines = {}
        for model, line in models.items():
            if isinstance(line, dict):
                extra = [field for field in line if field not in MODEL_FIELDS]
                if extra:
                    notes.append(paths.unknown_note(
                        f"{where} [{name}.models] {model!r}", extra))
                line = {f: v for f, v in line.items() if f not in extra}
            lines[model] = line
        entry["models"] = lines
    return entry


def renamed(entry):
    """A server's table, its models' lines too, under today's names; a
    line under its new name beats its old one."""
    if not isinstance(entry, dict):
        return entry
    lines = {RENAMED.get(k, k): v for k, v in entry.items() if k not in RENAMED}
    for old, new in RENAMED.items():
        if old in entry and new not in entry:
            lines[new] = entry[old]
    if isinstance(lines.get("models"), dict):
        lines["models"] = {model: renamed(line)
                           for model, line in lines["models"].items()}
    return lines


def check(name, entry):
    """What is wrong with a server's table, or None."""
    if not isinstance(entry, dict):
        return f"{name} isn't a server's table ([{name}] with its url)"
    for field, value in entry.items():
        kind = SERVER_FIELDS.get(field)
        if kind is None:
            return (f"[{name}] has no line {field!r}; known: "
                    f"{', '.join(SERVER_FIELDS)}")
        if not isinstance(value, kind) or (kind is int and value is True):
            return f"[{name}] {field} has the wrong type"
    if (entry.get("api") or DEFAULT) not in APIS:
        return f"[{name}] api is one of {', '.join(APIS)}"
    if entry.get("reply_api", DEFAULT) not in APIS:
        return f"[{name}] reply_api is one of {', '.join(APIS)}"
    version = entry.get("responses_since_version", [0, 0, 0])
    if len(version) != 3 or any(type(part) is not int or part < 0 for part in version):
        return f"[{name}] responses_since_version needs major, minor and patch numbers"
    if "search_api" in entry and entry["search_api"] not in SEARCH_APIS:
        return f"[{name}] search_api is one of {', '.join(SEARCH_APIS)}"
    for url, windows in entry.get("context_windows", {}).items():
        if not isinstance(windows, dict) or any(
            not model_id(model) or not token_count(window)
            for model, window in windows.items()
        ):
            return f"[{name}.context_windows] {url!r} needs model IDs and token counts"
    for model, line in entry.get("models", {}).items():
        if not isinstance(line, dict):
            return f"[{name}.models] {model!r} takes a table of its lines"
        for field, value in line.items():
            if field not in MODEL_FIELDS:
                return (f"[{name}.models] {model!r} has no line {field!r}; "
                        f"known: {', '.join(MODEL_FIELDS)}")
            if not isinstance(value, SERVER_FIELDS[field]):
                return f"[{name}.models] {model!r} {field} has the wrong type"
            if field == "reply_api" and value not in APIS:
                return (f"[{name}.models] {model!r} reply_api is one of "
                        f"{', '.join(APIS)}")
        problem = check_reasoning(line)
        if problem:
            return f"[{name}.models] {model!r} {problem}"
    if problem := check_reasoning(entry):
        return f"[{name}] {problem}"
    return None


def check_reasoning(entry):
    for field in ("reasoning_efforts", "chat_reasoning_efforts",
                  "responses_reasoning_efforts", "chat_tool_efforts"):
        if any(value not in THINKING_EFFORTS for value in entry.get(field, ())):
            return f"{field} contains an unknown thinking effort"
    if entry.get("chat_thinking_field") not in (
        None, "reasoning", "thinking", "enable_thinking",
    ):
        return "chat_thinking_field is reasoning, thinking or enable_thinking"
    return None


def without_endpoint_settings(entry):
    """Keep settings that can carry over to a different endpoint."""
    return {
        **{key: value for key, value in entry.items() if key not in ENDPOINT_FIELDS},
        "models": {
            model: {key: value for key, value in line.items()
                    if key not in LIMIT_FIELDS and key not in REPLY_FIELDS}
            for model, line in entry.get("models", {}).items()
        },
    }


def merge(shipped, mine, where):
    """The user's tables over the catalog's, a line at a time; a model's
    lines likewise. A server of their own needs its url."""
    table = {
        name: {**entry, "models": {
            model: {key: value for key, value in line.items()
                    if key not in LIMIT_FIELDS}
            for model, line in entry.get("models", {}).items()
        }}
        for name, entry in shipped.items()
    }
    for name, entry in mine.items():
        if name not in table:
            if "url" not in entry:
                raise ModelServerError(f"{where}: [{name}] needs its url")
            table[name] = dict(entry)
            continue
        previous = table[name]
        if endpoint(entry.get("url", previous["url"])) != endpoint(previous["url"]):
            previous = without_endpoint_settings(previous)
        models = {**previous.get("models", {})}
        for model, line in entry.get("models", {}).items():
            models[model] = {**models.get(model, {}), **line}
        table[name] = {**previous, **entry, "models": models}
    return table


def catalog():
    """The installed catalog, read once."""
    if "shipped" not in CACHE:
        CACHE["shipped"] = load(CATALOG, CATALOG)
    return CACHE["shipped"]


def table():
    """Every configured model server. Raises when models.toml can't be read."""
    shipped = catalog()
    path = paths.models_file()
    key = (path, stamp(path))
    if CACHE.get("key") != key:
        found = []
        CACHE["table"] = merge(shipped, load(path, path, found), path)
        CACHE["key"] = key
        CACHE["notes"] = found
        for note in found:
            print(f"ZERO: {note}", file=sys.stderr, flush=True)
    return CACHE["table"]


def notes():
    """What the last read of the user's models.toml didn't know of it."""
    table()
    return list(CACHE.get("notes", []))


def find(name):
    """A server by name, with no model's lines; None if there is none."""
    entry = table().get(name)
    return ModelServer(name, entry) if entry else None


def bare_server_name():
    """The catalog server that receives model names without a known prefix."""
    return next(name for name, entry in catalog().items()
                if entry.get("bare_model_names"))


def model_address(model, entries):
    """The catalog or configured server name and the model name it receives."""
    bare = bare_server_name()
    prefix, _, rest = model.partition("/")
    if rest and prefix in entries and prefix != bare:
        return prefix, rest
    return bare, model


def canonical_endpoint(name, url):
    shipped = catalog().get(name)
    return shipped is not None and endpoint(shipped["url"]) == endpoint(url)


def saved_window(entry, model):
    return entry.get("context_windows", {}).get(endpoint(entry["url"]), {}).get(model)


def model_limits(name, entry, model, *, refresh=True, reported=None):
    """Verified provider limits and the effective limits after user caps."""
    url = entry["url"]
    verified = reported if reported is not None else directory.reported(url, model)
    if canonical_endpoint(name, url):
        published = limit_catalog.lookup(
            name, model, refresh=refresh and verified.context_window is None,
        ) if name in limit_catalog.providers() else TokenLimits()
        shipped = TokenLimits.read(catalog()[name].get("models", {}).get(model))
        verified = verified.capped_by(published).capped_by(shipped)
    line = entry.get("models", {}).get(model, {})
    manual = saved_window(entry, model)
    # The old per-model setting took precedence over the server-wide setting.
    # A new endpoint-specific entry supersedes either, preserving both on disk.
    saved = manual or token_count(line.get("context_window")) or token_count(
        entry.get("context_window"),
    )
    configured = TokenLimits(
        saved,
        token_count(line.get("max_input_tokens", entry.get("max_input_tokens"))),
        token_count(line.get("max_output_tokens", entry.get("max_output_tokens"))),
    )
    return verified, verified.capped_by(configured)


def read_directory(name, entry):
    """Refresh deployed limits when a custom endpoint is used directly."""
    if canonical_endpoint(name, entry["url"]) and name in limit_catalog.providers():
        return
    server = ModelServer(name, entry)
    try:
        directory.listing(server.url, api_key(server), cached=True)
    except Exception:
        # Startup still uses verified and configured limits it already has.
        pass


def resolve(model, *, check_endpoint=False, refresh=True):
    """(server, the server's own model name) for a model name."""
    entries = table()
    name, served_name = model_address(model, entries)
    entry = entries[name]
    if check_endpoint:
        read_directory(name, entry)
    server = ModelServer(name, entry, served_name, refresh=refresh)
    if server.context_window is None:
        raise ModelServerError(
            f"no context window for {model}: set context_window under [{name}] (or "
            f"for the model under [{name}.models]) in {paths.models_file()}"
        )
    return server, served_name


def key_envs():
    """Every environment variable a server's key may come from: what no
    command may see. Raises ModelServerError when models.toml can't be read,
    since a variable it names can't be known."""
    return sorted({entry["key_env"] for entries in (catalog(), table())
                   for entry in entries.values() if "key_env" in entry})


def read_keys(path=None):
    """The saved key slots in ZERO's home."""
    path = Path(path or paths.keys_file())
    try:
        return tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ModelServerError(f"can't read {path}") from None


def stored_key(name, keys):
    slot = keys.get(name)
    key = slot.get("api_key") if isinstance(slot, dict) else None
    return key if isinstance(key, str) and key else None


def saved_key(server, keys):
    """Use a saved key at its recorded URL, or a legacy key on its catalog URL."""
    key = stored_key(server.name, keys)
    if key is None:
        return None
    url = keys[server.name].get("url")
    if isinstance(url, str) and endpoint(url) == endpoint(server.url):
        return key
    if url is None and canonical_endpoint(server.name, server.url):
        return key
    return None


def environment_key(server):
    return os.environ.get(server.key_env) if server.key_env else None


def api_key(server, keys_file=None):
    """The server's environment key, then its saved key, or its keyless marker."""
    key = environment_key(server)
    if key:
        return key
    keys = read_keys(keys_file)
    key = saved_key(server, keys)
    if key:
        return key
    if stored_key(server.name, keys):
        raise ModelServerError(f"Save an API key for {server.title}'s current URL.")
    if server.key_env is None:
        return NO_KEY
    raise ModelServerError(
        f"no API key for {server.name}: set {server.key_env}, or put "
        f"api_key under [{server.name}] in {keys_file or paths.keys_file()} (mode 600)"
    )


def make_client(model, timeout, *, server=None):
    """The SDK's client. Its timeout counts per read, a backstop for a
    connection that never opens; complete's clock is the one that counts."""
    server = server or resolve(model, check_endpoint=True)[0]
    # The SDK's own retries are off: hooks decide what to retry
    return OpenAI(
        base_url=server.reply_url,
        api_key=api_key(server),
        timeout=timeout,
        max_retries=0,
    )


class Model:
    """A model at its server, called through complete. `timeout`: the seconds
    it may stay silent (complete's `silence`), or a function giving them at
    each call (a session's limits as they stand)."""

    def __init__(self, name, client=None, *, timeout, effort=None, resolved=None):
        self.name = name
        self.timeout = timeout
        self.server, self.served_name = (
            resolved or resolve(name, check_endpoint=client is None)
        )
        self.api = self.server.api
        # What its calls cost: USD per 1M tokens (hit, miss, output)
        self.price = self.server.price
        # The SDK's client and the per-read timeout it was made with, made
        # again when the silence changes, so neither cuts the other short
        self._client_seconds = self.silence()
        self.client = client or make_client(
            name, self._client_seconds, server=self.server,
        )
        # The agent's thinking effort; the judge and the consent check call
        # complete themselves
        self.effort = effort
        # A stand-in client counts for itself, if it does
        self._scripted = client is not None
        self._counts = {}

    def count_tokens(self, text):
        """`text`'s tokens as this model's server counts them (/tokenize
        at the server's root), or None where it has no count; a
        server that doesn't answer is asked no more."""
        if self._scripted:
            count = getattr(self.client, "count_tokens", None)
            return count(text) if count else None
        if self.server.url in NO_COUNT:
            return None
        key = hash(text)
        if key not in self._counts:
            found = self._ask_count(text)
            if found is NONE:
                NO_COUNT.add(self.server.url)
                return None
            if found is None:
                return None
            if len(self._counts) >= COUNTS_KEPT:
                self._counts.clear()
            self._counts[key] = found
        return self._counts[key]

    def _ask_count(self, text):
        root = self.server.url.rstrip("/").removesuffix("/v1")
        body = json.dumps({"model": self.served_name, "content": text}).encode()
        request = urllib.request.Request(
            f"{root}/tokenize", body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=COUNT_SECONDS) as reply:
                tokens = json.load(reply).get("tokens")
        except urllib.error.HTTPError:
            # It answered, with no count to give
            return NONE
        except (OSError, ValueError):
            # Slow or gone this time: counted by bytes, and asked again later
            return None
        return len(tokens) if isinstance(tokens, list) else NONE

    def silence(self):
        """The seconds the model may stay silent now."""
        return self.timeout() if callable(self.timeout) else self.timeout

    def client_for(self, seconds):
        """The SDK's client for a call that may stay silent `seconds`: a
        stand-in as it is; a real one made again when the seconds changed."""
        if not self._scripted and seconds != self._client_seconds:
            self.client = make_client(self.name, seconds, server=self.server)
            self._client_seconds = seconds
        return self.client

    def json_request(self, schema):
        """How to ask this model for JSON fitting `schema` (features.py)."""
        return features.json_request(
            self.name, self.api, schema, route=self.server.history_route,
        )

    def call(self, messages, schemas, check=None, on_delta=None,
             max_output_tokens=None, reply_schema=None):
        """One model call, streamed (complete). API errors, a silence past the
        timeout and replies that didn't complete are raised for the loop to
        handle (MODEL_ERRORS); what `check` raises ends it at once. The
        effort is checked for the model, endpoint and attached tools.
        `max_output_tokens`: a cap on the
        reply (a compaction's handoff); None sends none."""
        seconds = self.silence()
        output = (features.json_request(
            self.name, self.api, reply_schema, unmeasured=False,
            route=self.server.history_route,
        ) if reply_schema else {})
        effort = features.request_effort(self.name, self.server, self.effort,
                                         tools=bool(schemas))
        replay_encrypted = self.server.replays_encrypted_reasoning
        ready = provider_history(messages, self.server.history_route)
        options = {}
        if self.api.NAME == "responses" and replay_encrypted:
            options["encrypted_reasoning"] = True
        if self.server.chat_thinking_field:
            options["thinking_field"] = self.server.chat_thinking_field
        reply = complete(
            self.api, self.client_for(seconds), self.served_name, ready,
            silence=seconds, check=check, on_delta=on_delta,
            tools=schemas, effort=effort,
            max_output_tokens=smaller(max_output_tokens, self.server.max_output_tokens),
            **self.progress(), **output, **options,
        )
        remember_provider_reply(reply, self.server.history_route,
                                response_items=self.server.replays_response_items)
        return reply

    def progress(self):
        """The prompt progress field, where this model is measured to honour
        it: a long prompt's reading then keeps the call alive."""
        if features.prefill_progress_sent(
            self.name, self.api, route=self.server.history_route,
        ):
            return {"prefill_progress": True}
        return {}
