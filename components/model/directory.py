"""Authenticated model directories, with endpoint-reported token limits."""

import json
import re
from concurrent.futures import Future
from dataclasses import dataclass, field
from hashlib import sha256
from threading import Lock
from time import monotonic
from urllib.parse import urljoin

import httpx2
from openai import OpenAI

import paths
from components.model.apis import chat_completions
from components.model.limits import TokenLimits, endpoint, model_id, token_count

LIST_SECONDS = 3
FRESH_SECONDS = 60
VERSION_SECONDS = 0.5
TOTAL_FIELDS = ("max_model_len", "context_length", "max_context_length")
FILE_FORMAT = 1


@dataclass
class Listing:
    reply: Future = field(default_factory=Future)
    read_at: float = 0.0
    reasoning: dict = field(default_factory=dict)


_listings = {}
_listing_lock = Lock()
_saved_reports = {}
_server_versions = {}
_server_version_lock = Lock()


def server_version(url):
    """Read a compatible server's release metadata without credentials."""
    url = urljoin(url, "/api/version")
    with _server_version_lock:
        checked_at, version = _server_versions.get(url, (-FRESH_SECONDS, None))
        if monotonic() - checked_at < FRESH_SECONDS:
            return version
        version = read_server_version(url)
        _server_versions[url] = (monotonic(), version)
        return version


def read_server_version(url):
    try:
        reply = httpx2.get(url, timeout=VERSION_SECONDS, trust_env=False)
        data = reply.raise_for_status().json()
    except (httpx2.HTTPError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not re.fullmatch(
        r"[0-9]{1,4}(\.[0-9]{1,4}){2}", version,
    ):
        return None
    return tuple(map(int, version.split(".")))


def route(url, key):
    return endpoint(url), sha256(key.encode()).digest()


def rows(url, key):
    with OpenAI(base_url=url, api_key=key, timeout=LIST_SECONDS,
                max_retries=0) as client:
        items = client.models.list().data
    found = []
    for item in items:
        if not model_id(item.id):
            continue
        data = item.model_dump()
        window, loads_window = chat_completions.runtime_context_window(data)
        window = token_count(window)
        if not loads_window:
            window = next((count for name in TOTAL_FIELDS
                           if (count := token_count(data.get(name)))), None)
        limits = TokenLimits.read({**data, "context_window": window})
        row = {"id": item.id, "context_window": window, **limits.as_dict()}
        reasoning = data.get("reasoning")
        if isinstance(reasoning, dict):
            row["reasoning"] = reasoning
        if loads_window:
            row["context_reported_on_load"] = True
        found.append(row)
    return found


def windows_file():
    """The last token limits an endpoint reported for its models."""
    return paths.home() / "state" / "model_windows.json"


def listing(url, key, *, cached=False):
    """Read /models once for concurrent callers of the same connection."""
    path = windows_file()
    address, fingerprint = route(url, key)
    identity = (path, address)
    with _listing_lock:
        previous, entry = _listings.get(identity, (None, None))
        if previous == fingerprint and (
            not entry.reply.done()
            or (cached and monotonic() - entry.read_at < FRESH_SECONDS)
        ):
            send = False
        else:
            entry = Listing()
            _listings[identity] = (fingerprint, entry)
            send = True
    if send:
        try:
            data = rows(url, key)
            remember(path, address, data, entry)
        except Exception as error:
            # A failed Future holds a traceback through its caller. Release
            # it after serving in-flight readers. Startup can reuse the empty
            # result for a minute. An explicit directory read retries.
            unavailable = Listing(read_at=monotonic())
            unavailable.reply.set_result([])
            with _listing_lock:
                if _listings.get(identity, (None, None))[1] is entry:
                    _listings[identity] = (fingerprint, unavailable)
            entry.reply.set_exception(error)
        else:
            entry.reasoning = {
                row["id"]: row["reasoning"] for row in data if "reasoning" in row
            }
            entry.read_at = monotonic()
            entry.reply.set_result(data)
    return entry.reply.result()


def read_reports(path):
    try:
        record = json.loads(path.read_bytes())
        if record.get(paths.FILE_FORMAT_KEY) == FILE_FORMAT:
            rows = record["endpoints"]
            if isinstance(rows, dict):
                return rows
    except (AttributeError, KeyError, OSError, ValueError):
        pass
    return {}


def reported(url, model):
    path = windows_file()
    rows = _saved_reports.get(path)
    if rows is None:
        rows = _saved_reports.setdefault(path, read_reports(path))
    models = rows.get(endpoint_key(url), {})
    return TokenLimits.read(models.get(model) if isinstance(models, dict) else None)


def reported_reasoning(url, model):
    """Exact-model metadata from the most recent directory for this endpoint."""
    identity = (windows_file(), endpoint(url))
    _, entry = _listings.get(identity, (None, None))
    return entry.reasoning.get(model) if entry is not None else None


def endpoint_key(url):
    # Keep user info and query strings out of the derived state file.
    return sha256(endpoint(url).encode()).hexdigest()


def remember(path, url, models, listing):
    reports = {}
    unloaded = set()
    for row in models:
        limits = TokenLimits.read(row).as_dict()
        if row.get("context_reported_on_load") and row["context_window"] is None:
            unloaded.add(row["id"])
        elif limits:
            reports[row["id"]] = limits
    if not reports and not unloaded:
        return
    try:
        with paths.changing(path):
            with _listing_lock:
                if _listings.get((path, url), (None, None))[1] is not listing:
                    return
            old = read_reports(path)
            name = endpoint_key(url)
            merged = {**old, name: {
                **{model: limits for model, limits in old.get(name, {}).items()
                   if model not in unloaded},
                **reports,
            }}
            _saved_reports[path] = merged
            if old != merged:
                record = {paths.FILE_FORMAT_KEY: FILE_FORMAT, "endpoints": merged}
                paths.write_whole(path, json.dumps(record, sort_keys=True))
    except OSError:
        # A cache failure does not discard the directory response.
        pass
