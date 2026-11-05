"""The model's part in the host (host/parts.py): `model/list`, which asks
every server ZERO can reach for its models, so any face lists them from the
host (the full screen's /model menu today); and connecting one (connect.py),
under `model/provider/`: `list` (every server and whether its key is there),
`probe` (one server's models, for a face's pick list), `connect` (a test call,
then saved) and `disconnect`. Each is whole in one request: the host keeps
nothing between a face's steps, so a plain command sends the same. Only
`model/list` and `model/provider/list` look; the rest are the user's own
face's alone, since they send a key or change the user's files.

A server is reachable when it needs no key or its key is there (providers.py);
the others aren't asked. Each is asked through the OpenAI chat API's
standard /v1/models, Ollama and llama-server alike, all at once, each for a
few seconds at most. A model is listed by the name a session takes:
"myserver/some-model", or a bare name for Ollama's.
"""

from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI

from host.parts import Part
from model import connect, providers

# Seconds a server has to list its models; one that doesn't answer says so
LIST_SECONDS = 3


class ModelList(Part):
    name = "models"
    looking = ("model/list", "model/provider/list")

    def __init__(self, config=None):
        # config.toml (settings.Config), where the default goes; the host's
        # once attached. None, or a host that reads none: no default is set
        self.config = config

    def attach(self, runtime):
        super().attach(runtime)
        if self.config is None:
            self.config = runtime.config

    def messages(self):
        return {"model/list": self.models_message,
                "model/provider/list": self.providers_message,
                "model/provider/probe": self.probe_message,
                "model/provider/connect": self.connect_message,
                "model/provider/disconnect": self.disconnect_message}

    def default(self):
        """New sessions' model, as the host has it."""
        if getattr(self, "runtime", None) is not None:
            return self.runtime.model
        return self.config()["model"] if callable(self.config) else None

    def writable(self):
        return self.config if hasattr(self.config, "set") else None

    def providers_message(self, request):
        try:
            listed = connect.rows()
        except providers.ProviderError as e:
            return {"ok": False, "error": str(e)}
        model = self.default()
        return {"ok": True, "providers": listed, "default": model,
                "ready": connect.ready(model)}

    def probe_message(self, request):
        try:
            return {"ok": True, **connect.probe_request(request)}
        except (connect.ConnectError, providers.ProviderError) as e:
            return {"ok": False, "error": str(e)}

    def connect_message(self, request):
        try:
            model = connect.connect(request)
        except (connect.ConnectError, providers.ProviderError, OSError) as e:
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
                     and connect.provider_of(config()["model"]) == name)
            connect.disconnect(name)
        except (connect.ConnectError, providers.ProviderError, OSError) as e:
            return {"ok": False, "error": str(e)}
        if unset:
            config.set("model", None)
        return {"ok": True, "removed": name, "default_unset": unset}

    def models_message(self, request):
        try:
            table = providers.table()
        except providers.ProviderError as e:
            return {"ok": False, "error": str(e)}
        asked = []
        for name in table:
            provider = providers.find(name)
            try:
                key = providers.api_key(provider)
            except providers.ProviderError:
                continue
            asked.append((provider, key))
        with ThreadPoolExecutor(max_workers=max(1, len(asked))) as pool:
            servers = list(pool.map(lambda pair: served(*pair), asked))
        return {"ok": True, "servers": servers}


def served(provider, key):
    """One server's row: its models by ZERO's names, or why there are none."""
    row = {"provider": provider.name, "title": provider.title, "models": []}
    client = OpenAI(base_url=provider.url, api_key=key,
                    timeout=LIST_SECONDS, max_retries=0)
    try:
        found = [item.id for item in client.models.list().data]
    except Exception as e:
        # Whatever the server or the network said: the row says it, the
        # other servers still list
        row["error"] = f"{type(e).__name__}: {e}"[:300]
        return row
    prefix = "" if provider.name == providers.BARE else f"{provider.name}/"
    row["models"] = [prefix + name for name in found]
    return row
