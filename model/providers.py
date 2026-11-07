"""Which model, at which server, with which key.

The providers come from two files: ZERO's catalog (catalog.toml beside this
file: the ones it ships with) and the user's models.toml (paths.py: the
servers they added, and any line of a shipped one they changed, by naming
the same provider). A model name picks its provider by its prefix
("deepseek/deepseek-flash", "myserver/some-model"); a name without a known
prefix goes to Ollama ("qwen3.5:9b"). A model's window is its own line, else
its provider's; a model with neither can't start. A provider's line `api`
names the API its server speaks (apis/), the Chat Completions API when it
names none.

An API key comes from the provider's environment variable or from the keys
file (paths.py), straight into the client; the harness never puts it in the
environment its commands inherit (key_envs: what the sandbox strips).
"""

import os
import tomllib
from pathlib import Path

from openai import OpenAI

import paths
from core.llm import complete
from model import features
from model.apis import APIS, DEFAULT

CATALOG = Path(__file__).with_name("catalog.toml")
# Where a name without a known provider's prefix goes
BARE = "ollama"
# A provider's lines, and the ones a model may have of its own
FIELDS = {
    "name": str, "url": str, "key_env": str, "api": str, "context": int,
    "context_stop": int, "price": list, "models": dict,
}
MODEL_FIELDS = ("context", "context_stop", "price")
# Lines a provider had before 0.6.2.7: what a model honours is measured now
RETIRED = ("json_format", "thinking")
# Where the context guard stops a turn when no line says
STOP = 0.8
# What a keyless server is sent for a key: the client needs one
NO_KEY = "none"


class ProviderError(Exception):
    """A model can't be reached as configured (no key, say): stop at startup."""


class Provider:
    """A provider as one of its models sees it: the provider's lines with the
    model's own over them."""

    def __init__(self, name, entry, model=None):
        line = entry.get("models", {}).get(model, {}) if model else {}
        merged = {**entry, **line}
        self.name = name
        # What the screens show
        self.title = entry.get("name", name)
        self.url = entry["url"]
        # Tokens the model takes, and where the context guard stops a turn;
        # None for a provider looked up without a model
        self.context = merged.get("context")
        self.context_stop = merged.get("context_stop")
        if self.context_stop is None and self.context is not None:
            self.context_stop = int(self.context * STOP)
        # The API its server speaks (apis/)
        self.api = APIS[entry.get("api", DEFAULT)]
        # None: no key needed
        self.key_env = entry.get("key_env")
        # USD per 1M tokens at peak price: (cache hit, cache miss, output)
        price = merged.get("price")
        self.price = tuple(price) if price else None


def load(path, where):
    """A providers file's tables, checked; no file, none."""
    try:
        data = tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ProviderError(f"can't read {where}: {e}") from e
    for name, entry in data.items():
        problem = check(name, entry)
        if problem:
            raise ProviderError(f"{where}: {problem}")
    return data


def check(name, entry):
    """What is wrong with a provider's table, or None."""
    if not isinstance(entry, dict):
        return f"{name} isn't a provider's table ([{name}] with its url)"
    for field, value in entry.items():
        kind = FIELDS.get(field)
        if field in RETIRED:
            return (f"[{name}] {field} is gone: what a model honours is "
                    "measured now (zero model features MODEL)")
        if kind is None:
            return (f"[{name}] has no line {field!r}; known: "
                    f"{', '.join(FIELDS)}")
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
            if not isinstance(value, FIELDS[field]):
                return f"[{name}.models] {model!r} {field} has the wrong type"
    return None


def merge(shipped, mine, where):
    """The user's tables over the catalog's, a line at a time; a model's
    lines likewise. A provider of their own needs its url."""
    table = {name: dict(entry) for name, entry in shipped.items()}
    for name, entry in mine.items():
        if name not in table:
            if "url" not in entry:
                raise ProviderError(f"{where}: [{name}] needs its url")
            table[name] = dict(entry)
            continue
        models = {**table[name].get("models", {})}
        for model, line in entry.get("models", {}).items():
            models[model] = {**models.get(model, {}), **line}
        table[name] = {**table[name], **entry, "models": models}
    return table


# The catalog, read once; the user's file again whenever it changes
CACHE = {}


def stamp(path):
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def table():
    """Every provider ZERO can reach now, by name. Raises ProviderError when
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
    """A provider by name, with no model's lines; None if there is none."""
    entry = table().get(name)
    return Provider(name, entry) if entry else None


def resolve(model):
    """(provider, the provider's own model name) for a model name."""
    providers = table()
    prefix, _, rest = model.partition("/")
    if rest and prefix in providers and prefix != BARE:
        name, api_name = prefix, rest
    else:
        name, api_name = BARE, model
    provider = Provider(name, providers[name], api_name)
    if provider.context is None:
        raise ProviderError(
            f"no context window for {model}: set context under [{name}] (or "
            f"for the model under [{name}.models]) in {paths.models_file()}"
        )
    return provider, api_name


def key_envs():
    """Every environment variable a provider's key may come from: what no
    command may see. Raises ProviderError when models.toml can't be read,
    since a variable it names can't be known."""
    return sorted({e["key_env"] for e in table().values() if "key_env" in e})


def api_key(provider, keys_file=None):
    """The provider's key: its environment variable, else the keys file;
    a provider that names no variable needs none."""
    key = os.environ.get(provider.key_env) if provider.key_env else None
    if key:
        return key
    keys_file = Path(keys_file or paths.keys_file())
    try:
        keys = tomllib.loads(keys_file.read_text())
    except FileNotFoundError:
        keys = {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ProviderError(f"can't read {keys_file}: {e}") from e
    key = keys.get(provider.name, {}).get("api_key")
    if key:
        return key
    if provider.key_env is None:
        return NO_KEY
    raise ProviderError(
        f"no API key for {provider.name}: set {provider.key_env}, or put "
        f"api_key under [{provider.name}] in {keys_file} (mode 600)"
    )


def make_client(model, timeout):
    """The SDK's client. Its timeout counts per read, a backstop for a
    connection that never opens; complete's clock is the one that counts."""
    provider, _ = resolve(model)
    # The SDK's own retries are off: plugins decide what to retry
    return OpenAI(
        base_url=provider.url,
        api_key=api_key(provider),
        timeout=timeout,
        max_retries=0,
    )


class Model:
    def __init__(self, name, client=None, *, timeout, effort=None):
        self.name = name
        # Seconds the model may stay silent (complete's `silence`)
        self.timeout = timeout
        self.provider, self.api_name = resolve(name)
        self.api = self.provider.api
        self.client = client or make_client(name, timeout)
        # The agent's thinking effort; the judge and the consent check call
        # complete themselves
        self.effort = effort

    def json_request(self, schema):
        """How to ask this model for JSON fitting `schema` (features.py)."""
        return features.json_request(self.name, self.api, schema)

    def call(self, messages, schemas, check=None, on_delta=None):
        """One model call, streamed (complete). API errors, a silence past the
        timeout and replies that didn't complete are raised for the loop to
        handle (MODEL_ERRORS); what `check` raises ends it at once. The
        effort is sent unless the model is measured not to honour it
        (features.py): kept, not sent."""
        return complete(
            self.api, self.client, self.api_name, messages,
            silence=self.timeout, check=check, on_delta=on_delta,
            tools=schemas,
            effort=features.effort_sent(self.name, self.api, self.effort),
        )
