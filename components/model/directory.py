"""Authenticated model directories, with endpoint-reported limits and capabilities."""

import json
import os
import re
from concurrent.futures import Future
from dataclasses import dataclass, field
from hashlib import sha256
from threading import Lock
from time import monotonic
from urllib.parse import urljoin

import httpx2

import paths
from components.model.apis import APIS, DEFAULT
from components.model.limits import TokenLimits, endpoint

LIST_SECONDS = 3
FRESH_SECONDS = 60
VERSION_SECONDS = 0.5
FILE_FORMAT = 2
LIMIT_KEYS = ("context_window", "max_input_tokens", "max_output_tokens")


@dataclass
class Listing:
    reply: Future = field(default_factory=Future)
    read_at: float = 0.0
    reasoning: dict = field(default_factory=dict)


_listings = {}
_listing_lock = Lock()
_saved_reports = {}
_reports_version = 0
_sig_cache = {}
_server_versions = {}
_server_version_lock = Lock()


def _bump_reports_version():
    global _reports_version
    _reports_version += 1


def reports_token():
    """Cheap monotonic revision token for persisted directory capability reports."""
    return (os.environ.get("ZERO_HOME"), _reports_version, len(_saved_reports))


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


def route(url, key, api=None):
    chosen = api or APIS[DEFAULT]
    return endpoint(url), (chosen.NAME, sha256(key.encode()).digest())


def rows(url, key, api=None):
    return (api or APIS[DEFAULT]).list_models(url, key, LIST_SECONDS)


def windows_file():
    """The last limits and capabilities an endpoint reported for its models."""
    return paths.home() / "state" / "model_windows.json"


def is_reply_capable(row):
    """False only when a listed model explicitly marks itself non-reply-capable."""
    return isinstance(row, dict) and row.get("reply_capable") is not False


def listing(url, key, *, api=None, cached=False, reply_only=True):
    """Read /models once for concurrent callers of the same connection."""
    path = windows_file()
    chosen = api or APIS[DEFAULT]
    address, fingerprint = route(url, key, chosen)
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
            data = rows(url, key, chosen)
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
    result = entry.reply.result()
    if reply_only:
        return [row for row in result if is_reply_capable(row)]
    return list(result)


def read_reports(path):
    """Read persisted endpoint metadata (accepting format-1 and format-2 files)."""
    try:
        record = json.loads(path.read_bytes())
        if record.get(paths.FILE_FORMAT_KEY) in (1, FILE_FORMAT):
            rows = record["endpoints"]
            if isinstance(rows, dict):
                return rows
    except (AttributeError, KeyError, OSError, ValueError):
        pass
    return {}


def _reports_for(path):
    global _reports_version
    rows = _saved_reports.get(path)
    if rows is None:
        rows = _saved_reports.setdefault(path, read_reports(path))
        _reports_version += 1
    return rows


def reported_entry(url, model):
    """Persisted metadata dict for `(url, model)`, or empty dict."""
    if not model:
        return {}
    rows = _reports_for(windows_file())
    models = rows.get(endpoint_key(url), {})
    entry = models.get(model) if isinstance(models, dict) else None
    return dict(entry) if isinstance(entry, dict) else {}


def reported(url, model):
    return TokenLimits.read(reported_entry(url, model))


def _capabilities_from_entry(entry):
    """Extract normalized capability dict from a persisted model entry."""
    if not entry:
        return {}
    caps = dict(entry.get("capabilities") or {})
    if "reasoning" not in caps:
        reasoning = entry.get("reasoning")
        if isinstance(reasoning, bool):
            caps["reasoning"] = reasoning
        elif isinstance(reasoning, dict) and isinstance(
            reasoning.get("supported"), bool,
        ):
            caps["reasoning"] = reasoning["supported"]
    if isinstance(entry.get("reply_capable"), bool):
        caps["reply_capable"] = entry["reply_capable"]
    methods = entry.get("supported_methods")
    if isinstance(methods, list) and methods:
        caps["supported_methods"] = [m for m in methods if isinstance(m, str)]
    return caps


def reported_capabilities(url, model):
    """Persisted endpoint capability dict for `(url, model)`."""
    return _capabilities_from_entry(reported_entry(url, model))


def capabilities_signature(url, model, *, ep_key=None):
    """Stable hashable signature of persisted capabilities for `(url, model)`."""
    if not model:
        return ""
    path = windows_file()
    rows = _reports_for(path)
    key = ep_key or endpoint_key(url)
    cache_key = (path, key, model)
    cached = _sig_cache.get(cache_key)
    if cached is not None and cached[0] is rows:
        return cached[1]
    models = rows.get(key, {})
    entry = models.get(model) if isinstance(models, dict) else None
    if not isinstance(entry, dict) or not entry:
        sig = ""
    else:
        caps = _capabilities_from_entry(entry)
        sig = (
            json.dumps(caps, sort_keys=True, separators=(",", ":"))
            if caps else ""
        )
    if len(_sig_cache) > 512:
        _sig_cache.clear()
    _sig_cache[cache_key] = (rows, sig)
    return sig


def reported_methods(url, model):
    """Persisted supported generation methods for `(url, model)`."""
    methods = reported_entry(url, model).get("supported_methods")
    if isinstance(methods, list):
        return tuple(m for m in methods if isinstance(m, str))
    return ()


def reported_reasoning(url, model):
    """Exact-model reasoning metadata from the directory for this endpoint."""
    identity = (windows_file(), endpoint(url))
    _, entry = _listings.get(identity, (None, None))
    if entry is not None and model in entry.reasoning:
        return entry.reasoning.get(model)
    saved = reported_entry(url, model).get("reasoning")
    return saved if isinstance(saved, dict) else None


def endpoint_key(url):
    # Keep user info and query strings out of the derived state file.
    return sha256(endpoint(url).encode()).hexdigest()


def _extract_record(row):
    """Extract persistable limits, capabilities, reasoning, and methods."""
    record = TokenLimits.read(row).as_dict()
    capabilities = row.get("capabilities")
    if isinstance(capabilities, dict) and capabilities:
        record["capabilities"] = capabilities
    reasoning = row.get("reasoning")
    if isinstance(reasoning, (dict, bool)):
        record["reasoning"] = reasoning
    methods = row.get("supported_methods")
    if isinstance(methods, list):
        clean = [m for m in methods if isinstance(m, str)]
        if clean:
            record["supported_methods"] = clean
    if row.get("reply_capable") is False:
        record["reply_capable"] = False
    return record


def _merge_entry(previous, update, *, drop_limits=False):
    """Merge updated model metadata over a previously persisted entry."""
    base = dict(previous) if isinstance(previous, dict) else {}
    if drop_limits:
        for key in LIMIT_KEYS:
            base.pop(key, None)
    if not update:
        return base
    merged = {**base, **update}
    if isinstance(base.get("capabilities"), dict) and isinstance(
        update.get("capabilities"), dict,
    ):
        prev_caps = dict(base["capabilities"])
        new_caps = update["capabilities"]
        for key, value in new_caps.items():
            if (
                key in ("input", "output")
                and isinstance(prev_caps.get(key), dict)
                and isinstance(value, dict)
            ):
                prev_caps[key] = {**prev_caps[key], **value}
            else:
                prev_caps[key] = value
        merged["capabilities"] = prev_caps
    return merged


def remember(path, url, models, listing):
    reports = {}
    unloaded = set()
    for row in models:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        is_unloaded = (
            bool(row.get("context_reported_on_load"))
            and row.get("context_window") is None
        )
        if is_unloaded:
            unloaded.add(row["id"])
        record = _extract_record(row)
        if is_unloaded:
            for key in LIMIT_KEYS:
                record.pop(key, None)
        if record:
            reports[row["id"]] = record
    if not reports and not unloaded:
        return
    try:
        with paths.changing(path):
            with _listing_lock:
                if _listings.get((path, url), (None, None))[1] is not listing:
                    return
            old = read_reports(path)
            name = endpoint_key(url)
            existing = old.get(name, {})
            updated = {}
            for model, prev in existing.items():
                if model in unloaded:
                    kept = _merge_entry(prev, reports.get(model), drop_limits=True)
                    if kept:
                        updated[model] = kept
                else:
                    updated[model] = prev
            for model, rec in reports.items():
                if model not in unloaded:
                    updated[model] = _merge_entry(existing.get(model), rec)
            merged = {**old, name: updated}
            _saved_reports[path] = merged
            _bump_reports_version()
            if old != merged:
                record = {paths.FILE_FORMAT_KEY: FILE_FORMAT, "endpoints": merged}
                paths.write_whole(path, json.dumps(record, sort_keys=True))
    except OSError:
        # A cache failure does not discard the directory response.
        pass


def remember_model(url, model, metadata):
    """Persist lazily discovered single-model limits and capabilities."""
    if not model or not isinstance(metadata, dict):
        return
    record = _extract_record(metadata)
    if not record:
        return
    path = windows_file()
    address = endpoint(url)
    try:
        with paths.changing(path):
            old = read_reports(path)
            name = endpoint_key(address)
            existing = dict(old.get(name, {}))
            existing[model] = _merge_entry(existing.get(model), record)
            merged = {**old, name: existing}
            _saved_reports[path] = merged
            _bump_reports_version()
            if old != merged:
                payload = {paths.FILE_FORMAT_KEY: FILE_FORMAT, "endpoints": merged}
                paths.write_whole(path, json.dumps(payload, sort_keys=True))
    except OSError:
        pass
