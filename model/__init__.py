"""The model: which one, at which server, with which key, retried how.

servers.py: the servers (catalog.toml, the ones ZERO ships with, and the
user's models.toml), a model name's server, its key and its client, and
`Model`, whose `.call` the loop uses. service.py: the server's service that lists
every server's models. retry.py: the hook that retries a call that failed
for a temporary reason. The streamed call itself is core's (core/llm.py).
"""

from settings import Setting

# The agent's thinking effort, least to most
THINKING_EFFORTS = ("off", "low", "high", "max")
# The feature of the model's API each effort needs (features.py)
THINKING_EFFORT_FEATURES = {"off": "thinking_off", "low": "thinking_on",
                            "high": "thinking_on", "max": "thinking_on"}
THINKING_EFFORT_NEEDS = tuple(THINKING_EFFORT_FEATURES.items())

SETTINGS = (
    # The user's default: none until they connect a model; ZERO's code has
    # none of its own
    Setting("model", "Default", "Model", None, "model",
            unset_means="none set"),
    # The model a call goes to when the session's own fails past its
    # retries (a server down): that turn goes on with it, the next turn
    # tries the session's own again; a judge on the failing model asks it
    # too. None: a failed call ends the turn
    Setting("fallback_model", "Default", "Fallback model", None, "model",
            unset_means="none"),
    # The agent's own calls only; the consent check stays on the API's
    # default
    Setting("thinking_effort", "Default", "Thinking effort", "high",
            "choice", choices=THINKING_EFFORTS, needs=THINKING_EFFORT_NEEDS,
            runs_on="model"),
    # Silence, not the whole call: before the first token and between
    # tokens, a server's progress through the prompt counting as life where
    # the model honours it (features.py). A backstop for a server stuck
    # silent: Codex waits 300 s, opencode 900 (DeepSeek asks for 15
    # minutes), Hermes 180 and 900 for a local server (8 Nov); 45 s failed
    # LT-21's healthy reading of 143k tokens
    Setting("model_silence_seconds", "Limits", "Seconds the model may stay silent", 300,
            "number"),
    Setting("model_retries", "Limits", "Retries after a model error", 3, "int",
            minimum=0),
    # About this, twice that, four times that between retries
    Setting("model_retry_seconds", "Limits", "Seconds before the first retry", 1.0,
            "number"),
)
