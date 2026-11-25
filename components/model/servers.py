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

import paths
from core.llm import complete
from components.model import (
    MODALITIES,
    ModelCapabilities,
    THINKING_EFFORTS,
    directory,
    features,
    limit_catalog,
)
from components.model.apis import (
    APIS,
    DEFAULT,
    IMAGE_APIS,
    SEARCH_APIS,
    STT_APIS,
    TTS_APIS,
    VIDEO_APIS,
    adapter_capabilities,
    chat_completions,
)
from components.model.apis.conversation import ProjectionCache
from components.model.apis.multimodal import (
    has_conversation_attachments,
    with_max_output_tokens,
)
from components.model.limits import (
    TokenLimits, endpoint, model_id, smaller, token_count,
)
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
    "stt_api": str, "stt_model": str, "stt_live_model": str,
    "tts_api": str, "tts_model": str,
    "image_api": str, "image_model": str,
    "video_api": str, "video_model": str,
    "max_reasoning_effort": str, "context_windows": dict,
    "max_input_tokens": int, "max_output_tokens": int,
    "models_dev_provider": str,
    "reply_api": str, "responses_url": str, "chat_thinking_field": str,
    "responses_since_version": list,
    "reasoning_efforts": list, "chat_reasoning_efforts": list,
    "responses_reasoning_efforts": list,
    "chat_tool_efforts": list, "accepts_zero_temperature": bool,
    "replays_encrypted_reasoning": bool, "replays_response_items": bool,
    "requests_reasoning_summary": bool, "reports_reasoning_efforts": bool,
    "suggested_models": list,
    "capabilities": dict, "supports_model_show": bool,
}
MODEL_FIELDS = ("context_window", "max_input_tokens", "max_output_tokens",
                "context_stop", "price", "max_reasoning_effort", "reply_api",
                "reasoning_efforts", "chat_reasoning_efforts", "chat_tool_efforts",
                "responses_reasoning_efforts",
                "accepts_zero_temperature", "requests_reasoning_summary",
                "chat_thinking_field", "capabilities")
LIMIT_FIELDS = ("context_window", "max_input_tokens", "max_output_tokens")
REPLY_FIELDS = (
    "reply_api", "responses_url", "responses_since_version",
    "reasoning_efforts", "chat_reasoning_efforts",
    "responses_reasoning_efforts",
    "chat_tool_efforts", "max_reasoning_effort", "accepts_zero_temperature",
    "replays_encrypted_reasoning", "replays_response_items",
    "requests_reasoning_summary", "chat_thinking_field",
    "reports_reasoning_efforts",
    "suggested_models",
    "capabilities",
)
ENDPOINT_FIELDS = (
    "key_env", "search_api", "search_model",
    "stt_api", "stt_model", "stt_live_model",
    "tts_api", "tts_model",
    "image_api", "image_model",
    "video_api", "video_model",
    "refuses_overlong_requests", *LIMIT_FIELDS, *REPLY_FIELDS,
)
CAPABILITY_KEYS = frozenset({
    "input", "output", "reasoning", "tool_calling", "reply_capable",
})
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
        self.reports_reasoning_efforts = entry.get("reports_reasoning_efforts", False)
        self.reasoning_efforts = merged.get("reasoning_efforts")
        self.tool_efforts = None
        self.chat_thinking_field = None
        if self.api.NAME == "chat_completions":
            self.reasoning_efforts = merged.get(
                "chat_reasoning_efforts", self.reasoning_efforts,
            )
            self.tool_efforts = merged.get("chat_tool_efforts")
            self.chat_thinking_field = merged.get("chat_thinking_field")
        elif self.api.NAME == "responses":
            self.reasoning_efforts = merged.get(
                "responses_reasoning_efforts", self.reasoning_efforts,
            )
        if (self.reasoning_efforts is None and model
                and canonical_endpoint(name, self.url)
                and name in limit_catalog.providers()
                and self.reported_reasoning() is None
                and limit_catalog.reasoning_supported(name, model) is False):
            self.reasoning_efforts = []
        self.max_reasoning_effort = merged.get("max_reasoning_effort")
        if (self.api.NAME != "responses"
                and "responses_reasoning_efforts" in line
                and "reasoning_efforts" not in line):
            self.max_reasoning_effort = None
        self.accepts_zero_temperature = merged.get("accepts_zero_temperature", True)
        self.replays_encrypted_reasoning = entry.get(
            "replays_encrypted_reasoning", False,
        )
        self.replays_response_items = entry.get("replays_response_items", True)
        self.requests_reasoning_summary = merged.get(
            "requests_reasoning_summary", False,
        )
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
        self._endpoint_key = directory.endpoint_key(self.url) if self.url else ""
        self._caps_reports_token = None
        self._caps_sig = ""
        self._caps_cache = {}
        self.capabilities = self.capabilities_for("chat", refresh=refresh)
        self._caps_key = self._capability_revision()

    def _capability_revision(self):
        token = directory.reports_token()
        if token != self._caps_reports_token:
            self._caps_sig = (
                directory.capabilities_signature(
                    self.url, self._model, ep_key=self._endpoint_key,
                )
                if self._model else ""
            )
            self._caps_reports_token = directory.reports_token()
        return (CACHE.get("key"), self._reported, self._caps_sig)

    def set_limits(self, reported, *, refresh=False):
        entry, model = self._entry, self._model
        self._reported = reported
        self._caps_cache = {}
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
        if hasattr(self, "api"):
            self.capabilities = self.capabilities_for("chat", refresh=False)
            self._caps_key = self._capability_revision()

    def refresh_limits(self):
        """Apply new endpoint observations or saved window edits to an open session."""
        try:
            entry = table().get(self.name)
        except ModelServerError:
            entry = None
        if entry is not None and endpoint(entry["url"]) == endpoint(self.url):
            entry_changed = entry != self._entry
            self._entry = entry
        else:
            entry_changed = False
        reported = directory.reported(self.url, self._model)
        if entry_changed or reported != self._reported:
            self.set_limits(reported)
        else:
            caps_key = self._capability_revision()
            if getattr(self, "_caps_key", None) != caps_key:
                self._caps_cache = {}
                self.capabilities = self.capabilities_for("chat", refresh=False)
                self._caps_key = caps_key

    def capabilities_for(self, role="chat", *, api=None, refresh=False):
        """Effective passive ModelCapabilities for `(model, endpoint, api, role)`."""
        use_api = api or self.api
        rev = self._capability_revision()
        cache_key = (role, use_api, rev)
        if not refresh:
            cached = self._caps_cache.get(cache_key)
            if cached is not None:
                return cached
        if getattr(self, "_caps_key", None) != rev:
            self._caps_cache = {}
            self._caps_key = rev
        caps = model_capabilities(
            self.name,
            self._entry,
            self._model,
            api=use_api,
            role=role,
            refresh=refresh,
            reported=self._reported if self._model else None,
        )
        self._caps_cache[cache_key] = caps
        if role == "chat" and use_api is self.api:
            self.capabilities = caps
        return caps

    def reported_reasoning(self):
        if self.reports_reasoning_efforts:
            return directory.reported_reasoning(self.url, self._model)
        return None

    def request_options(self, api=None, *, encrypted=False, zero_temperature=False):
        """Request kwargs for `api` (defaults to `self.api`) from server flags."""
        return (api or self.api).request_options(
            self, encrypted=encrypted, zero_temperature=zero_temperature,
        )

    def keeps_continuation(self, api=None):
        """Whether to keep route-scoped continuation items on `api`."""
        return (api or self.api).keeps_continuation(self)


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
    for api_field, registry in (
        ("search_api", SEARCH_APIS),
        ("stt_api", STT_APIS),
        ("tts_api", TTS_APIS),
        ("image_api", IMAGE_APIS),
        ("video_api", VIDEO_APIS),
    ):
        if api_field in entry and entry[api_field] not in registry:
            known = ", ".join(registry) if registry else "(none registered)"
            return f"[{name}] {api_field} is one of {known}"
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
        problem = check_capabilities(line)
        if problem:
            return f"[{name}.models] {model!r} {problem}"
    if problem := check_reasoning(entry):
        return f"[{name}] {problem}"
    if problem := check_capabilities(entry):
        return f"[{name}] {problem}"
    return None


def check_capabilities(entry):
    """Validate optional `capabilities` table on a server or model entry."""
    caps = entry.get("capabilities")
    if caps is None:
        return None
    if not isinstance(caps, dict):
        return "capabilities must be a table"
    for key, value in caps.items():
        if key not in CAPABILITY_KEYS:
            return (
                f"capabilities has unknown key {key!r}; known: "
                f"{', '.join(sorted(CAPABILITY_KEYS))}"
            )
        if key in ("input", "output"):
            if isinstance(value, dict):
                for modality, flag in value.items():
                    if modality not in MODALITIES:
                        return (
                            f"capabilities.{key} has unknown modality "
                            f"{modality!r}"
                        )
                    if not isinstance(flag, bool):
                        return (
                            f"capabilities.{key}.{modality} must be a boolean"
                        )
            elif isinstance(value, list):
                if any(
                    not isinstance(item, str) or item not in MODALITIES
                    for item in value
                ):
                    return (
                        f"capabilities.{key} list may only name "
                        f"{', '.join(MODALITIES)}"
                    )
            else:
                return f"capabilities.{key} must be a table or list"
        elif not isinstance(value, bool):
            return f"capabilities.{key} must be a boolean"
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
        mine = load(path, path, found)
        CACHE["mine"] = mine
        CACHE["table"] = merge(shipped, mine, path)
        CACHE["key"] = key
        CACHE["notes"] = found
        for note in found:
            print(f"ZERO: {note}", file=sys.stderr, flush=True)
    return CACHE["table"]


def user_table():
    """The user's own models.toml tables before merging over catalog.toml."""
    table()
    return CACHE.get("mine", {})


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


def window_details(name, entry, model, *, refresh=True, reported=None):
    """Verified limits, effective limits, and the source of the effective window."""
    url = entry["url"]
    endpoint_limits = (
        reported if reported is not None else directory.reported(url, model)
    )
    catalog_limits = TokenLimits()
    if canonical_endpoint(name, url):
        published = limit_catalog.lookup(
            name, model,
            refresh=refresh and endpoint_limits.context_window is None,
        ) if name in limit_catalog.providers() else TokenLimits()
        shipped = TokenLimits.read(catalog()[name].get("models", {}).get(model))
        catalog_limits = published.capped_by(shipped)
    verified = endpoint_limits.capped_by(catalog_limits)
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
    limits = verified.capped_by(configured)
    if limits.context_window is None:
        source = "unknown"
    elif saved is not None and (
        verified.context_window is None or saved <= verified.context_window
    ):
        source = "saved"
    elif endpoint_limits.context_window is not None and (
        catalog_limits.context_window is None
        or endpoint_limits.context_window <= catalog_limits.context_window
    ):
        source = "endpoint"
    else:
        source = "catalog"
    return verified, limits, source


def model_limits(name, entry, model, *, refresh=True, reported=None):
    """Verified provider limits and the effective limits after user caps."""
    verified, limits, _ = window_details(
        name, entry, model, refresh=refresh, reported=reported,
    )
    return verified, limits


def _merge_caps_dict(base, override):
    """Merge model-level capabilities over server-level capabilities."""
    if not base and not override:
        return {}
    merged = dict(base or {})
    for key, value in (override or {}).items():
        if (
            key in ("input", "output")
            and isinstance(merged.get(key), dict)
            and isinstance(value, dict)
        ):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _efforts_reasoning(entry, line, api_name):
    """Infer explicit reasoning bool from declared effort lists when absent."""
    merged = {**(entry or {}), **(line or {})}
    efforts = merged.get("reasoning_efforts")
    if api_name == "chat_completions":
        efforts = merged.get("chat_reasoning_efforts", efforts)
    elif api_name == "responses":
        efforts = merged.get("responses_reasoning_efforts", efforts)
    if efforts is None:
        return None
    return bool(efforts)


def _entry_caps(entry, model, api_name):
    """Extract a capability dict from one server/model config layer."""
    if not isinstance(entry, dict):
        return {}
    line = entry.get("models", {}).get(model, {}) if model else {}
    caps = _merge_caps_dict(entry.get("capabilities"), line.get("capabilities"))
    if "reasoning" not in caps:
        inferred = _efforts_reasoning(entry, line, api_name)
        if inferred is not None:
            caps["reasoning"] = inferred
    return caps


def _user_caps(name, entry, model, api_name):
    """Extract highest-precedence user_config capabilities."""
    try:
        mine = user_table().get(name)
    except ModelServerError:
        mine = None
    if mine is not None:
        return _entry_caps(mine, model, api_name)
    shipped = catalog().get(name)
    if shipped is None or not canonical_endpoint(name, entry["url"]):
        return _entry_caps(entry, model, api_name)
    # Direct synthetic override passed to ModelServer without models.toml.
    entry_caps = _entry_caps(entry, model, api_name)
    shipped_caps = _entry_caps(shipped, model, api_name)
    if entry_caps != shipped_caps:
        return entry_caps
    return {}


def model_capabilities(
    name, entry, model, *, api=None, role="chat", refresh=False, reported=None,
):
    """Compute effective passive ModelCapabilities for `(model, endpoint, api, role)`.

    effective = adapter_declared ∩ resolved(
        user_config > endpoint_metadata > catalog.toml > published_catalog > unknown
    )
    """
    line = entry.get("models", {}).get(model, {}) if model else {}
    chosen_api = api or reply_api(name, entry, model, line)
    adapter = adapter_capabilities(chosen_api, role=role)
    url = entry["url"]

    user_layer = _user_caps(name, entry, model, chosen_api.NAME)
    endpoint_layer = directory.reported_capabilities(url, model) if model else {}
    catalog_layer = {}
    published_layer = None
    if canonical_endpoint(name, url):
        shipped = catalog().get(name)
        catalog_layer = _entry_caps(shipped, model, chosen_api.NAME)
        if model and name in limit_catalog.providers():
            published_layer = limit_catalog.capabilities(name, model)

    if model is not None:
        _, limits = model_limits(
            name, entry, model, refresh=refresh, reported=reported,
        )
    else:
        limits = TokenLimits.read(entry)

    return ModelCapabilities.resolve(
        adapter,
        user_layer,
        endpoint_layer,
        catalog_layer,
        published_layer,
        context_window=limits.context_window,
        max_input_tokens=limits.max_input_tokens,
        max_output_tokens=limits.max_output_tokens,
        supported_methods=directory.reported_methods(url, model) if model else (),
        role=role,
        api=chosen_api.NAME,
    )


def _has_configured_window(entry, model):
    line = entry.get("models", {}).get(model, {}) if model else {}
    return bool(
        saved_window(entry, model)
        or token_count(line.get("context_window"))
        or token_count(entry.get("context_window"))
    )


def read_directory(name, entry, model=None):
    """Refresh deployed limits and lazy model metadata on endpoint selection."""
    is_published = (
        canonical_endpoint(name, entry["url"]) and name in limit_catalog.providers()
    )
    if is_published:
        return
    server = ModelServer(name, entry, refresh=False)
    try:
        key = api_key(server)
    except ModelServerError:
        return
    listed_ok = False
    try:
        directory.listing(server.url, key, api=server.api, cached=True)
        listed_ok = True
    except Exception:
        # Startup still uses verified and configured limits it already has.
        pass
    if model is None or server.api.NAME not in ("chat_completions", "responses"):
        return
    wants_show = bool(entry.get("supports_model_show")) or (
        listed_ok
        and directory.reported(server.url, model).context_window is None
        and not _has_configured_window(entry, model)
    )
    if not wants_show:
        return
    shown = chat_completions.show_model(server.url, model, key)
    if shown:
        directory.remember_model(server.url, model, shown)


def resolve(model, *, check_endpoint=False, refresh=True):
    """(server, the server's own model name) for a model name."""
    entries = table()
    name, served_name = model_address(model, entries)
    entry = entries[name]
    if check_endpoint:
        read_directory(name, entry, served_name)
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
    """The saved key slots in ZERO's home, cached by file mtime and size."""
    path = paths.keys_file() if path is None else Path(path)
    key = (path, stamp(path))
    cached = CACHE.get("keys")
    if cached is not None and cached[0] == key:
        return cached[1]
    if key[1] is None:
        data = {}
        CACHE["keys"] = (key, data)
        return data
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        data = {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ModelServerError(f"can't read {path}") from None
    CACHE["keys"] = (key, data)
    return data


def entry_api_key(name, entry, keys):
    """Fast key lookup for a catalog/models.toml entry without building ModelServer."""
    key_env = entry.get("key_env")
    if key_env:
        env_key = os.environ.get(key_env)
        if env_key and env_key != NO_KEY:
            return env_key
    key = stored_key(name, keys)
    if key is None or key == NO_KEY:
        return None
    entry_url = entry.get("url")
    if not isinstance(entry_url, str) or not entry_url:
        return None
    saved_url = keys[name].get("url")
    if isinstance(saved_url, str) and endpoint(saved_url) == endpoint(entry_url):
        return key
    if saved_url is None and canonical_endpoint(name, entry_url):
        return key
    return None


def utility_services(capability, discover_fn, *, extra_key=None):
    """Cached utility discovery keyed by models.toml, keys.toml, and key_env values."""
    tbl = table()
    try:
        keys = read_keys()
    except ModelServerError:
        keys = {}
    env_vals = tuple(
        (name, os.environ.get(entry["key_env"]))
        for name, entry in tbl.items()
        if "key_env" in entry
    )
    cache_key = (capability, id(tbl), id(keys), env_vals, extra_key)
    bucket = CACHE.setdefault("utility_services", {})
    cached = bucket.get(capability)
    if cached is not None and cached[0] == cache_key:
        return dict(cached[1])
    found = discover_fn(
        capability,
        tbl,
        lambda name, entry: entry_api_key(name, entry, keys),
    )
    bucket[capability] = (cache_key, found)
    return dict(found)


def stored_key(name, keys):
    entry = keys.get(name)
    key = entry.get("api_key") if isinstance(entry, dict) else None
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
    """The API adapter's client. Its timeout counts per read, a backstop for a
    connection that never opens; complete's clock is the one that counts."""
    server = server or resolve(model, check_endpoint=True)[0]
    return server.api.make_client(server.reply_url, api_key(server), timeout)


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
        self.projections = ProjectionCache()

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

    def request_options(self, *, encrypted=False, zero_temperature=False):
        """Request kwargs for this model's API from its server capabilities."""
        return self.server.request_options(
            self.api, encrypted=encrypted, zero_temperature=zero_temperature,
        )

    def json_request(self, schema):
        """How to ask this model for JSON fitting `schema` (features.py)."""
        return features.json_request(
            self.name, self.api, schema, route=self.server.history_route,
        )

    @property
    def capabilities(self):
        """Effective passive ModelCapabilities for this model's chat role."""
        return self.capabilities_for("chat")

    def capabilities_for(self, role="chat"):
        """Effective passive ModelCapabilities for `(model, endpoint, api, role)`."""
        return self.server.capabilities_for(role, api=self.api)

    def _native_count_client(self, seconds):
        """Return a client suitable for native input-token counting, or None."""
        client = self.client_for(seconds)
        if not self._scripted:
            return client
        if (getattr(client, "supports_native_counting", False)
                or hasattr(client, "post_json")
                or (hasattr(client, "post") and hasattr(client, "base_url"))):
            return client
        return None

    def prepare_request(self, conversation, schemas, *, max_output_tokens=None,
                        tool_choice=None, media_budget=None, total_budget=None):
        """Build the exact route-specific prepared request and accounting plan."""
        seconds = self.silence()
        effort = features.request_effort(self.name, self.server, self.effort,
                                         tools=bool(schemas))
        capped = smaller(max_output_tokens, self.server.max_output_tokens)
        return self.api.prepare_request(
            self.served_name,
            conversation,
            route=self.server.history_route,
            project=self.projections.project,
            tools=schemas,
            tool_choice=tool_choice,
            effort=effort,
            max_output_tokens=capped,
            capabilities=self.capabilities,
            media_budget=media_budget,
            total_budget=total_budget,
            client=self._native_count_client(seconds),
            **self.progress(),
            **self.request_options(encrypted=True),
        )

    def call(self, conversation, schemas, check=None, on_delta=None,
             max_output_tokens=None, tool_choice=None, prepared=None):
        """One model call, streamed (complete). API errors, a silence past the
        timeout and replies that didn't complete are raised for the loop to
        handle (MODEL_ERRORS); what `check` raises ends it at once. The
        effort is checked for the model, endpoint and attached tools.
        `max_output_tokens`: a cap on the reply (a compaction's handoff); None
        sends none. `tool_choice`: optional native tool-choice directive (e.g.
        "none" for cache-preserving handoffs). `prepared`: optional pre-built
        PreparedRequest so the exact counted request is sent."""
        seconds = self.silence()
        capped = smaller(max_output_tokens, self.server.max_output_tokens)
        if prepared is not None:
            body = with_max_output_tokens(self.api.NAME, prepared.body, capped)
            return complete(
                self.api, self.client_for(seconds), self.served_name, conversation,
                route=self.server.history_route,
                keep_continuation=self.server.keeps_continuation(self.api),
                silence=seconds, check=check, on_delta=on_delta,
                prepared_body=body,
            )
        effort = features.request_effort(self.name, self.server, self.effort,
                                         tools=bool(schemas))
        extra = {}
        if has_conversation_attachments(conversation):
            extra["capabilities"] = self.capabilities
            count_client = self._native_count_client(seconds)
            if count_client is not None:
                extra["client"] = count_client
        return complete(
            self.api, self.client_for(seconds), self.served_name, conversation,
            route=self.server.history_route,
            keep_continuation=self.server.keeps_continuation(self.api),
            project=self.projections.project,
            silence=seconds, check=check, on_delta=on_delta,
            tools=schemas, tool_choice=tool_choice, effort=effort,
            max_output_tokens=capped,
            **extra, **self.progress(), **self.request_options(encrypted=True),
        )

    def progress(self):
        """The prompt progress field, where this model is measured to honour
        it: a long prompt's reading then keeps the call alive."""
        if features.prefill_progress_sent(
            self.name, self.api, route=self.server.history_route,
        ):
            return {"prefill_progress": True}
        return {}
