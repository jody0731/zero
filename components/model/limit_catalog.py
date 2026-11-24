"""Published limits from models.dev and the snapshot ZERO ships."""

import json
import tomllib
import urllib.request
from concurrent.futures import Future, TimeoutError
from functools import cache
from pathlib import Path
from threading import Lock, Thread
from time import monotonic, time

import paths
from components.model.limits import TokenLimits, model_id

SNAPSHOT = Path(__file__).with_name("limits.toml")
URL = "https://models.dev/api.json"
FRESH_SECONDS = 24 * 60 * 60
RETRY_SECONDS = 5 * 60
FETCH_SECONDS = 3
SELECTION_WAIT_SECONDS = 0.75
MAX_CATALOG_BYTES = 16 * 1024 * 1024
FILE_FORMAT = 2


def fetch_catalog(timeout=FETCH_SECONDS, url=URL):
    """Request only the public catalog. The request carries no model data."""
    request = urllib.request.Request(url, headers={"User-Agent": "ZERO model catalog"})
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        body = reply.read(MAX_CATALOG_BYTES + 1)
    if len(body) > MAX_CATALOG_BYTES:
        raise ValueError("model catalog exceeds its size limit")
    return json.loads(body)


@cache
def providers():
    """The external IDs for the providers in ZERO's configured catalog."""
    source = SNAPSHOT.with_name("catalog.toml")
    table = tomllib.loads(source.read_text(encoding="utf-8"))
    return {name: entry["models_dev_provider"] for name, entry in table.items()
            if "models_dev_provider" in entry}


def published_limits(catalog):
    """Read v1 models.dev token fields for the exact providers and IDs."""
    found = {}
    if not isinstance(catalog, dict):
        return found
    for server, provider in providers().items():
        record = catalog.get(provider)
        if not isinstance(record, dict) or record.get("id") != provider:
            continue
        rows = record.get("models")
        if not isinstance(rows, dict):
            continue
        models = {}
        for name, row in rows.items():
            if not model_id(name) or not isinstance(row, dict) or row.get("id") != name:
                continue
            limits = row.get("limit")
            if not isinstance(limits, dict):
                continue
            model = TokenLimits.read({
                "context_window": limits.get("context"),
                "max_input_tokens": limits.get("input"),
                "max_output_tokens": limits.get("output"),
            })
            if model.context_window is not None:
                entry = model.as_dict()
                if row.get("reasoning") is False:
                    entry["reasoning"] = False
                models[name] = entry
        if models:
            found[server] = models
    return found


@cache
def bundled():
    return tomllib.loads(SNAPSHOT.read_text(encoding="utf-8")), SNAPSHOT.stat().st_mtime


def _row(rows, name, model):
    server = rows.get(name)
    if isinstance(server, dict):
        row = server.get(model)
        if isinstance(row, dict):
            return row
    return None


def matching(rows, name, model):
    return TokenLimits.read(_row(rows, name, model))


class LimitCatalog:
    """Serve known entries immediately. One bounded wait on a cold miss."""

    def __init__(self, path):
        self.path = path
        self.lock = Lock()
        self.rows = None
        self.read_at = 0
        self.try_after = 0
        self.pending = None

    def lookup(self, name, model, *, refresh=True):
        baseline, shipped_at = bundled()
        with self.lock:
            if self.rows is None:
                self.rows, self.read_at = self.read()
            known = matching(self.rows, name, model).capped_by(
                matching(baseline, name, model),
            )
            stale = time() - (self.read_at or shipped_at) >= FRESH_SECONDS
            if refresh and (stale or known.context_window is None):
                self.refresh()
            pending = self.pending
        if known.context_window is not None or pending is None or not refresh:
            return known
        try:
            return matching(pending.result(timeout=SELECTION_WAIT_SECONDS), name, model)
        except TimeoutError:
            return known

    def reasoning(self, name, model):
        """Return False when models.dev explicitly marks the model non-reasoning."""
        baseline, _ = bundled()
        with self.lock:
            if self.rows is None:
                self.rows, self.read_at = self.read()
            cached = _row(self.rows, name, model)
        if cached is not None:
            return False if cached.get("reasoning") is False else None
        shipped = _row(baseline, name, model)
        if shipped is not None:
            return False if shipped.get("reasoning") is False else None
        return None

    def read(self):
        try:
            record = json.loads(self.path.read_bytes())
            if record.get(paths.FILE_FORMAT_KEY) == FILE_FORMAT:
                rows, read_at = record["providers"], self.path.stat().st_mtime
                if isinstance(rows, dict):
                    return rows, read_at
        except (AttributeError, KeyError, OSError, ValueError):
            pass
        return {}, 0

    def refresh(self):
        """Called under the lock. Throttle misses and failures as well as hits."""
        now = monotonic()
        if self.pending is not None or now < self.try_after:
            return
        self.pending = Future()
        self.try_after = now + RETRY_SECONDS
        Thread(target=self.fetch, args=(self.pending,), daemon=True).start()

    def fetch(self, pending):
        try:
            rows = published_limits(fetch_catalog())
        except (OSError, ValueError, RecursionError):
            rows = {}
        if rows:
            record = {paths.FILE_FORMAT_KEY: FILE_FORMAT, "providers": rows}
            try:
                with paths.changing(self.path):
                    paths.write_whole(self.path, json.dumps(record, sort_keys=True))
            except OSError:
                # The in-memory snapshot still serves this process.
                pass
        with self.lock:
            if rows:
                self.rows = rows
                self.read_at = time()
            self.pending = None
            pending.set_result(self.rows)


_catalogs = {}
_catalogs_lock = Lock()


def catalog_file():
    """The last public token catalog lookup, reused offline."""
    return paths.home() / "state" / "model_catalog.json"


def _catalog():
    path = catalog_file()
    with _catalogs_lock:
        catalog = _catalogs.get(path)
        if catalog is None:
            catalog = _catalogs[path] = LimitCatalog(path)
    return catalog


def lookup(name, model, *, refresh):
    return _catalog().lookup(name, model, refresh=refresh)


def reasoning_supported(name, model):
    return _catalog().reasoning(name, model)
