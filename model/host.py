"""The model's part in the host (host/parts.py): the `models` verb, which
asks every server ZERO can reach for its models, so any face lists them
from the host (the CLI's /model menu today).

A server is reachable when it needs no key or its key is there (providers.py);
the others aren't asked. Each is asked through the OpenAI chat API's
standard /v1/models, Ollama and llama-server alike, all at once, each for a
few seconds at most. A model is listed by the name a session takes:
"myserver/some-model", or a bare name for Ollama's.
"""

from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI

from host.parts import Part
from model import providers

# Seconds a server has to list its models; one that doesn't answer says so
LIST_SECONDS = 3


class ModelList(Part):
    name = "models"
    looking = ("models",)

    def verbs(self):
        return {"models": self.models_verb}

    def models_verb(self, request):
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
