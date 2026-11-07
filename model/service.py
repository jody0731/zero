"""The model's service in the server (server/services.py): `model/list`, which asks
every server ZERO can reach for its models, so any face lists them from the
server (the full screen's /model menu today); `model/features`, what a model
honours of its API's features (features.py), measured when none are kept or
when asked; and connecting one (connect.py), under `model/server/`: `list`
(every server and whether its key is there), `probe` (one server's models,
for a face's pick list), `connect` (a test call, then saved) and
`disconnect`. Each is whole in one request: the server keeps nothing between a
face's steps, so a plain command sends the same. Only `model/list` and
`model/server/list` look; the rest are the user's own face's alone, since
they send a key, change the user's files or call a model.

A server is reachable when it needs no key or its key is there (servers.py);
the others aren't asked. Each is asked through the OpenAI APIs'
standard /v1/models, Ollama and llama-server alike, all at once, each for a
few seconds at most. A model is listed by the name a session takes:
"myserver/some-model", or a bare name for Ollama's.
"""

from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI

from server.services import Service
from model import connect, features, servers

# Seconds a server has to list its models; one that doesn't answer says so
LIST_SECONDS = 3
# Seconds a model may stay silent while its features are measured: a local
# server may first load it
FEATURE_SECONDS = 60


class ModelService(Service):
    name = "models"
    looking_messages = ("model/list", "model/server/list")

    def __init__(self, config=None):
        # config.toml (settings.Config), where the default goes; the server's
        # once attached. None, or a server that reads none: no default is set
        self.config = config

    def attach(self, runtime):
        super().attach(runtime)
        if self.config is None:
            self.config = runtime.config

    def messages(self):
        return {"model/list": self.models_message,
                "model/features": self.features_message,
                "model/server/list": self.servers_message,
                "model/server/probe": self.probe_message,
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
            if row is None or request.get("test") is True:
                client = servers.make_client(model, FEATURE_SECONDS)
                row = features.measure(model, api, client, server_model,
                                       FEATURE_SECONDS)
        except (servers.ModelServerError, features.FeatureError) as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "model": model, "api": api.NAME,
                "measured_at": row["measured_at"],
                "features": {name: row["features"].get(name)
                             for name in features.FEATURES}}

    def servers_message(self, request):
        try:
            listed = connect.rows()
        except servers.ModelServerError as e:
            return {"ok": False, "error": str(e)}
        model = self.default()
        return {"ok": True, "servers": listed, "default": model,
                "not_ready_reason": connect.not_ready_reason(model)}

    def probe_message(self, request):
        try:
            return {"ok": True, **connect.probe_request(request)}
        except (connect.ConnectError, servers.ModelServerError) as e:
            return {"ok": False, "error": str(e)}

    def connect_message(self, request):
        try:
            model = connect.connect(request)
        except (connect.ConnectError, servers.ModelServerError, OSError) as e:
            return {"ok": False, "error": str(e)}
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
            connect.disconnect(name)
        except (connect.ConnectError, servers.ModelServerError, OSError) as e:
            return {"ok": False, "error": str(e)}
        if unset:
            config.set("model", None)
        return {"ok": True, "removed": name, "default_unset": unset}

    def models_message(self, request):
        try:
            table = servers.table()
        except servers.ModelServerError as e:
            return {"ok": False, "error": str(e)}
        asked = []
        for name in table:
            server = servers.find(name)
            try:
                key = servers.api_key(server)
            except servers.ModelServerError:
                continue
            asked.append((server, key))
        with ThreadPoolExecutor(max_workers=max(1, len(asked))) as pool:
            rows = list(pool.map(lambda pair: server_row(*pair), asked))
        return {"ok": True, "servers": rows}


def server_row(server, key):
    """One server's row: its models by ZERO's names, or why there are none."""
    row = {"server": server.name, "title": server.title, "models": []}
    client = OpenAI(base_url=server.url, api_key=key,
                    timeout=LIST_SECONDS, max_retries=0)
    try:
        found = [item.id for item in client.models.list().data]
    except Exception as e:
        # Whatever the server or the network said: the row says it, the
        # other servers still list
        row["error"] = f"{type(e).__name__}: {e}"[:300]
        return row
    prefix = "" if server.name == servers.UNPREFIXED_SERVER else f"{server.name}/"
    row["models"] = [prefix + name for name in found]
    return row
