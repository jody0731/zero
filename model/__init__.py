"""The model: which one, at which server, with which key, retried how.

providers.py: the providers (catalog.toml, the ones ZERO ships with, and the
user's models.toml), a model name's provider, its key and its client, and
`Model`, whose `.call` the loop uses. host.py: the host's part that lists
every server's models. retry.py: the plugin that retries a call that failed
for a temporary reason. The streamed call itself is core's (core/llm.py).
"""

from settings import Setting

# The agent's thinking effort, least to most
EFFORTS = ("off", "low", "high", "max")
# The feature of the model's API each effort needs (features.py)
EFFORT_FEATURES = {"off": "thinking_off", "low": "thinking_on",
                   "high": "thinking_on", "max": "thinking_on"}
EFFORT_NEEDS = tuple(EFFORT_FEATURES.items())

SETTINGS = (
    # The user's default: none until they connect a model; ZERO's code has
    # none of its own
    Setting("model", "Default", "Model", None, "model",
            off="none set"),
    # The agent's own calls only; the consent check stays on the API's
    # default
    Setting("effort", "Default", "Thinking effort", "high",
            "choice", choices=EFFORTS, needs=EFFORT_NEEDS, runs_on="model"),
    # Silence, not the whole call: before the first token and between
    # tokens
    Setting("model_timeout", "Limits", "Seconds the model may stay silent", 45,
            "number"),
    Setting("model_retries", "Limits", "Retries after a model error", 3, "int",
            least=0),
    # About this, twice that, four times that between retries; left out of
    # the view, since it has no visible effect
    Setting("retry_base", "Limits", "Seconds before the first retry", 1.0,
            "number", shown=False),
)
