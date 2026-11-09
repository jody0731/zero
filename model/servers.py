"""Which model, at which server, with which key.

The servers come from two files: ZERO's catalog (catalog.toml beside this
file: the ones it ships with) and the user's models.toml (paths.py: the
servers they added, and any line of a shipped one they changed, by naming
the same server). A model name picks its server by its prefix
("deepseek/deepseek-flash", "myserver/some-model"); a name without a known
prefix goes to Ollama ("qwen3.5:9b"). A model's context window is its own
line, else its server's; a model with neither can't start. A server's line `api`
names the API its server speaks (apis/), the Chat Completions API when it
names none.

An API key comes from the server's environment variable or from the keys
file (paths.py), straight into the client; the harness never puts it in the
environment its commands inherit (key_envs: what the sandbox strips).
"""

import json
import os
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from openai import OpenAI

import paths
from core.llm import complete
from model import features
from model.apis import APIS, DEFAULT
from settings import stamp

CATALOG = Path(__file__).with_name("catalog.toml")
# Where a name without a known server's prefix goes
UNPREFIXED_SERVER = "ollama"
# A server's lines, and the ones a model may have of its own. context_stop
# set where the context guard stopped a turn until 0.6.3; read and left
# alone since, as the line is compact_at_tokens (context/)
SERVER_FIELDS = {
    "title": str, "url": str, "key_env": str, "api": str,
    "context_window": int, "context_stop": int, "price": list, "models": dict,
}
MODEL_FIELDS = ("context_window", "context_stop", "price")
# Lines named otherwise before 0.6.2.8, read under their names now
RENAMED = {"name": "title", "context": "context_window"}
# Lines a server had before 0.6.2.7: what a model honours is measured now
RETIRED = ("json_format", "thinking")
# What a keyless server is sent for a key: the client needs one
NO_KEY = "none"
# Servers whose root answers no /tokenize (llama.cpp's), by url: asked once
NO_COUNT = set()
# Seconds a token count may take; past it, counted by bytes this time
COUNT_SECONDS = 10
# A server's answer with no count in it
NONE = object()
# Counts kept by the text's hash, so a measure asks once for each text
COUNTS_KEPT = 512


class ModelServerError(Exception):
    """A model can't be reached as configured (no key, say): stop at startup."""


class ModelServer:
    """A server as one of its models sees it: the server's lines with the
    model's own over them."""

    def __init__(self, name, entry, model=None):
        line = entry.get("models", {}).get(model, {}) if model else {}
        merged = {**entry, **line}
        self.name = name
        # What the screens show
        self.title = entry.get("title", name)
        self.url = entry["url"]
        # Tokens the model takes; None for a server looked up without a model
        self.context_window = merged.get("context_window")
        # The API its server speaks (apis/)
        self.api = APIS[entry.get("api", DEFAULT)]
        # None: no key needed
        self.key_env = entry.get("key_env")
        # USD per 1M tokens at peak price: (cache hit, cache miss, output)
        price = merged.get("price")
        self.price = tuple(price) if price else None


def load(path, where):
    """A servers file's tables, checked; no file, none."""
    try:
        data = tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ModelServerError(f"can't read {where}: {e}") from e
    data = {name: renamed(entry) for name, entry in data.items()}
    for name, entry in data.items():
        problem = check(name, entry)
        if problem:
            raise ModelServerError(f"{where}: {problem}")
    return data


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
        if field in RETIRED:
            return (f"[{name}] {field} is gone: what a model honours is "
                    "measured now (zero model features MODEL)")
        if kind is None:
            return (f"[{name}] has no line {field!r}; known: "
                    f"{', '.join(SERVER_FIELDS)}")
        if not isinstance(value, kind) or (kind is int and value is True):
            return f"[{name}] {field} has the wrong type"
    if entry.get("api", DEFAULT) not in APIS:
        return f"[{name}] api is one of {', '.join(APIS)}"
    for model, line in entry.get("models", {}).items():
        if not isinstance(line, dict):
            return f"[{name}.models] {model!r} takes a table of its lines"
        for field, value in line.items():
            if field not in MODEL_FIELDS:
                return (f"[{name}.models] {model!r} has no line {field!r}; "
                        f"known: {', '.join(MODEL_FIELDS)}")
            if not isinstance(value, SERVER_FIELDS[field]):
                return f"[{name}.models] {model!r} {field} has the wrong type"
    return None


def merge(shipped, mine, where):
    """The user's tables over the catalog's, a line at a time; a model's
    lines likewise. A server of their own needs its url."""
    table = {name: dict(entry) for name, entry in shipped.items()}
    for name, entry in mine.items():
        if name not in table:
            if "url" not in entry:
                raise ModelServerError(f"{where}: [{name}] needs its url")
            table[name] = dict(entry)
            continue
        models = {**table[name].get("models", {})}
        for model, line in entry.get("models", {}).items():
            models[model] = {**models.get(model, {}), **line}
        table[name] = {**table[name], **entry, "models": models}
    return table


# The catalog, read once; the user's file again whenever it changes
CACHE = {}


def table():
    """Every server ZERO can reach now, by name. Raises ModelServerError when
    the user's models.toml can't be used."""
    if "shipped" not in CACHE:
        CACHE["shipped"] = load(CATALOG, CATALOG)
    path = paths.models_file()
    key = (path, stamp(path))
    if CACHE.get("key") != key:
        CACHE["table"] = merge(CACHE["shipped"], load(path, path), path)
        CACHE["key"] = key
    return CACHE["table"]


def find(name):
    """A server by name, with no model's lines; None if there is none."""
    entry = table().get(name)
    return ModelServer(name, entry) if entry else None


def resolve(model):
    """(server, the server's own model name) for a model name."""
    servers = table()
    prefix, _, rest = model.partition("/")
    if rest and prefix in servers and prefix != UNPREFIXED_SERVER:
        name, served_name = prefix, rest
    else:
        name, served_name = UNPREFIXED_SERVER, model
    server = ModelServer(name, servers[name], served_name)
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
    return sorted({e["key_env"] for e in table().values() if "key_env" in e})


def api_key(server, keys_file=None):
    """The server's key: its environment variable, else the keys file;
    a server that names no variable needs none."""
    key = os.environ.get(server.key_env) if server.key_env else None
    if key:
        return key
    keys_file = Path(keys_file or paths.keys_file())
    try:
        keys = tomllib.loads(keys_file.read_text())
    except FileNotFoundError:
        keys = {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ModelServerError(f"can't read {keys_file}: {e}") from e
    key = keys.get(server.name, {}).get("api_key")
    if key:
        return key
    if server.key_env is None:
        return NO_KEY
    raise ModelServerError(
        f"no API key for {server.name}: set {server.key_env}, or put "
        f"api_key under [{server.name}] in {keys_file} (mode 600)"
    )


def make_client(model, timeout):
    """The SDK's client. Its timeout counts per read, a backstop for a
    connection that never opens; complete's clock is the one that counts."""
    server, _ = resolve(model)
    # The SDK's own retries are off: hooks decide what to retry
    return OpenAI(
        base_url=server.url,
        api_key=api_key(server),
        timeout=timeout,
        max_retries=0,
    )


class Model:
    """A model at its server, called through complete. `timeout`: the seconds
    it may stay silent (complete's `silence`), or a function giving them at
    each call (a session's limits as they stand)."""

    def __init__(self, name, client=None, *, timeout, effort=None):
        self.name = name
        self.timeout = timeout
        self.server, self.served_name = resolve(name)
        self.api = self.server.api
        # What its calls cost: USD per 1M tokens (hit, miss, output)
        self.price = self.server.price
        # The SDK's client and the per-read timeout it was made with, made
        # again when the silence changes, so neither cuts the other short
        self._client_seconds = self.silence()
        self.client = client or make_client(name, self._client_seconds)
        # The agent's thinking effort; the judge and the consent check call
        # complete themselves
        self.effort = effort
        # A stand-in client counts for itself, if it does
        self._scripted = client is not None
        self._counts = {}

    def count_tokens(self, text):
        """`text`'s tokens as this model's server counts them (llama.cpp's
        /tokenize at the server's root), or None where it has no count; a
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
            self.client = make_client(self.name, seconds)
            self._client_seconds = seconds
        return self.client

    def json_request(self, schema):
        """How to ask this model for JSON fitting `schema` (features.py)."""
        return features.json_request(self.name, self.api, schema)

    def call(self, messages, schemas, check=None, on_delta=None,
             max_output_tokens=None):
        """One model call, streamed (complete). API errors, a silence past the
        timeout and replies that didn't complete are raised for the loop to
        handle (MODEL_ERRORS); what `check` raises ends it at once. The
        effort is sent unless the model is measured not to honour it
        (features.py): kept, not sent. `max_output_tokens`: a cap on the
        reply (a compaction's handoff); None sends none."""
        seconds = self.silence()
        return complete(
            self.api, self.client_for(seconds), self.served_name, messages,
            silence=seconds, check=check, on_delta=on_delta,
            tools=schemas,
            effort=features.thinking_effort_sent(self.name, self.api, self.effort),
            max_output_tokens=max_output_tokens,
            **self.progress(),
        )

    def progress(self):
        """The prompt progress field, where this model is measured to honour
        it: a long prompt's reading then keeps the call alive."""
        if features.prefill_progress_sent(self.name, self.api):
            return {"prefill_progress": True}
        return {}
