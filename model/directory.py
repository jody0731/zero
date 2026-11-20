"""Authenticated model directories, with endpoint-reported token limits."""

import json
from concurrent.futures import Future
from dataclasses import dataclass, field
from hashlib import sha256
from threading import Lock
from time import monotonic

from openai import OpenAI

import paths
from model.apis import chat_completions
from model.limits import TokenLimits, endpoint, model_id, token_count

LIST_SECONDS = 3
FRESH_SECONDS = 60
TOTAL_FIELDS = ("max_model_len", "context_length", "max_context_length")
FILE_FORMAT = 1


@dataclass
class Listing:
    reply: Future = field(default_factory=Future)
    read_at: float = 0.0


_listings = {}
_listing_lock = Lock()
_saved_reports = {}


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
        if loads_window:
            row["context_reported_on_load"] = True
        found.append(row)
    return found


def listing(url, key, *, cached=False):
    """Read /models once for concurrent callers of the same connection."""
    path = paths.model_windows_file()
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
    path = paths.model_windows_file()
    rows = _saved_reports.get(path)
    if rows is None:
        rows = _saved_reports.setdefault(path, read_reports(path))
    models = rows.get(endpoint_key(url), {})
    return TokenLimits.read(models.get(model) if isinstance(models, dict) else None)


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
