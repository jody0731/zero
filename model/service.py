"""Model directories, saved connections and explicit tests for the apps.

Picker reads can reuse directories for the current URL and credential. A key
save refreshes just that server's directory. The directory read is separate
from connecting, testing and measuring models.
"""

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from hashlib import sha256
from threading import Lock
from time import monotonic

from openai import NotFoundError

from server.services import Service
from model import connect, features, servers
from model.limits import endpoint

# Seconds a model may stay silent while its features are measured: a local
# server may first load it (a backstop, as model_silence_seconds)
FEATURE_SECONDS = 300
# A picker asks again on demand once its last directory read has aged.
DIRECTORY_FRESH_SECONDS = 60


@dataclass
class ModelDirectory:
    route: tuple[str, bytes]
    reply: Future = field(default_factory=Future)
    read_at: float = 0.0


class ModelService(Service):
    name = "models"
    looking_messages = ("model/list", "model/server/list", "model/window")

    def __init__(self, config=None):
        # config.toml (settings.Config), where the default goes; the server's
        # once attached. None, or a server that reads none: no default is set
        self.config = config
        self.directories = {}
        self.directory_lock = Lock()

    def attach(self, runtime):
        super().attach(runtime)
        if self.config is None:
            self.config = runtime.config

    def messages(self):
        return {"model/list": self.models_message,
                "model/window": self.window_message,
                "model/window/set": self.set_window_message,
                "model/features": self.features_message,
                "model/server/list": self.servers_message,
                "model/server/probe": self.probe_message,
                "model/server/key/set": self.set_key_message,
                "model/server/test": self.test_message,
                "model/server/connect": self.connect_message,
                "model/server/disconnect": self.disconnect_message}

    def default(self):
        """New sessions' model, as the server has it."""
        if getattr(self, "runtime", None) is not None:
            return self.runtime.model
        return self.config()["model"] if callable(self.config) else None

    def writable(self):
        return self.config if hasattr(self.config, "set") else None

    def features_message(self, request):
        model = request.get("model")
        if not isinstance(model, str) or not model.strip():
            return {"ok": False, "error": "model/features needs a model"}
        model = model.strip()
        try:
            server, server_model = servers.resolve(model)
            api = server.api
            row = features.record(model, api)
            if request.get("cached_only"):
                row = row or {"features": {}}
            elif row is None or request.get("test") is True:
                client = servers.make_client(model, FEATURE_SECONDS)
                row = features.measure(model, api, client, server_model,
                                       FEATURE_SECONDS)
        except (servers.ModelServerError, features.FeatureError) as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "model": model, "api": api.NAME,
                "measured_at": row.get("measured_at"),
                "features": {name: row["features"].get(name)
                             for name in features.FEATURES}}

    def servers_message(self, request):
        try:
            listed = connect.rows()
        except servers.ModelServerError as e:
            return {"ok": False, "error": str(e)}
        model = self.default()
        return {"ok": True, "servers": listed, "default": model,
                "default_server": connect.server_of(model),
                "not_ready_reason": connect.not_ready_reason(model)}

    def probe_message(self, request):
        try:
            return {"ok": True, **connect.probe_request(request)}
        except (connect.ConnectError, servers.ModelServerError) as e:
            return {"ok": False, "error": str(e)}

    def window_message(self, request):
        try:
            return {"ok": True, **connect.window_request(request)}
        except (connect.ConnectError, servers.ModelServerError) as error:
            return {"ok": False, "error": str(error)}

    def set_window_message(self, request):
        try:
            connect.set_window_request(request)
        except (connect.ConnectError, servers.ModelServerError, OSError) as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True}

    def set_key_message(self, request):
        if "key" not in request:
            return {"ok": False, "error": "Supply a key or choose to remove it."}
        name, value = request.get("name"), request["key"]
        try:
            with self.directory_lock:
                server = connect.set_key(name, value)
                row = empty_directory(server)
                try:
                    key = servers.api_key(server)
                except servers.ModelServerError as error:
                    self.directories.pop(name, None)
                    if value is not None:
                        row["error"] = str(error)
                    pending = None
                else:
                    if value is None:
                        row = self.after_key_removal(server, key)
                        pending = None
                    else:
                        pending = self.directory_request(server, key, cached=False)
        except (connect.ConnectError, servers.ModelServerError) as error:
            return {"ok": False, "error": str(error)}
        except OSError:
            return {"ok": False, "error": "ZERO could not write the saved key."}
        if pending is not None:
            row = self.directory_result(pending)
        elif row["models"]:
            row = resolved_directory(server, row)
        return {"ok": True, "has_saved_key": value is not None, "directory": row}

    def test_message(self, request):
        try:
            return {"ok": True, "model": connect.test_request(request)}
        except (connect.ConnectError, servers.ModelServerError) as error:
            return connect_failure(error)

    def connect_message(self, request):
        try:
            model = connect.connect(request)
        except (connect.ConnectError, servers.ModelServerError, OSError) as error:
            return connect_failure(error)
        with self.directory_lock:
            self.directories.pop(request["name"], None)
        config = self.writable()
        made_default = False
        if config is not None and (request.get("default") is True
                                   or not config()["model"]):
            problem = config.set("model", model)
            if problem:
                return {"ok": False, "error": f"saved {model}, but not as the "
                        f"default: {problem}"}
            made_default = True
        return {"ok": True, "model": model, "default": made_default}

    def disconnect_message(self, request):
        name = request.get("name")
        config = self.writable()
        try:
            unset = (config is not None and config()["model"] is not None
                     and connect.server_of(config()["model"]) == name)
            with self.directory_lock:
                connect.disconnect(name)
                self.directories.pop(name, None)
        except (connect.ConnectError, servers.ModelServerError, OSError) as e:
            return {"ok": False, "error": str(e)}
        if unset:
            config.set("model", None)
        return {"ok": True, "removed": name, "default_unset": unset}

    def models_message(self, request):
        name = request.get("server")
        asked = []
        with self.directory_lock:
            try:
                table = servers.table()
            except servers.ModelServerError as error:
                return {"ok": False, "error": str(error)}
            if name is not None and (not isinstance(name, str) or name not in table):
                return {"ok": False, "error": "Choose a configured model server."}
            for server_name in (table if name is None else (name,)):
                server = servers.ModelServer(server_name, table[server_name])
                try:
                    key = servers.api_key(server)
                except servers.ModelServerError as error:
                    self.directories.pop(server_name, None)
                    if name is not None:
                        return {"ok": True, "servers": [
                            {**empty_directory(server), "error": str(error)},
                        ]}
                    continue
                asked.append(self.directory_request(
                    server, key, cached=request.get("cached") is True,
                ))
        with ThreadPoolExecutor(max_workers=max(1, len(asked))) as pool:
            rows = list(pool.map(self.directory_result, asked))
        return {"ok": True, "servers": rows}

    def directory_request(self, server, key, *, cached):
        """Reserve one lookup under the lock. Concurrent reads can share it."""
        route = directory_route(server, key)
        previous = self.directories.get(server.name)
        if previous and previous.route == route:
            fresh = cached and monotonic() - previous.read_at < DIRECTORY_FRESH_SECONDS
            if fresh or not previous.reply.done():
                return server, key, previous, False
        directory = ModelDirectory(route)
        self.directories[server.name] = directory
        return server, key, directory, True

    def directory_result(self, pending):
        server, key, directory, send = pending
        if send:
            row = server_row(server, key)
            directory.read_at = monotonic()
            directory.reply.set_result(row)
        while True:
            row = directory.reply.result()
            with self.directory_lock:
                current = self.directories.get(server.name)
            if current is None:
                return empty_directory(server)
            if current is directory:
                return resolved_directory(server, row)
            # Rotating or removing a key can finish before an old lookup.
            directory = current

    def after_key_removal(self, server, key):
        """Keep a directory already read through a still-effective credential."""
        previous = self.directories.get(server.name)
        if previous and previous.route == directory_route(server, key):
            return (previous.reply.result() if previous.reply.done()
                    else empty_directory(server))
        self.directories.pop(server.name, None)
        return empty_directory(server)


def connect_failure(error):
    reply = {"ok": False, "error": str(error)}
    if report := getattr(error, "model_failure", None):
        reply["model_failure"] = report
    return reply


def directory_route(server, key):
    return server.url, sha256(key.encode()).digest()


def empty_directory(server):
    return {"server": server.name, "title": server.title, "models": []}


def server_row(server, key):
    """One server's row: its models by ZERO's names, or why there are none."""
    row = empty_directory(server)
    try:
        found = connect.listing(server.url, key)
        row["models"] = [connect.full_name(server.name, model["id"]) for model in found]
    except NotFoundError as error:
        row["error"] = ("This server has no model directory at its configured API URL. "
                        + connect.error_text(error, key))
    except Exception as error:
        row["error"] = (str(error) if isinstance(error, connect.ConnectError)
                        else connect.error_text(error, key))
    return row


def resolved_directory(server, row):
    """Join available limits to the endpoint's roster without any catalog waits."""
    if not row["models"]:
        return row
    try:
        entry = servers.table().get(server.name)
    except servers.ModelServerError:
        return row
    if entry is None or endpoint(entry["url"]) != endpoint(server.url):
        return row
    prefix = connect.full_name(server.name, "")
    found = {}
    for model in row["models"]:
        _, limits = servers.model_limits(
            server.name, entry, model.removeprefix(prefix), refresh=False,
        )
        if fields := limits.as_dict():
            found[model] = fields
    return {**row, "model_limits": found} if found else row
