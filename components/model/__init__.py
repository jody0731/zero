"""The model: which one, at which server, with which key, retried how.

servers.py: the servers (catalog.toml, the ones ZERO ships with, and the
user's models.toml), a model name's server, its key and its client, and
`Model`, whose `.call` the loop uses. service.py: the server's service that lists
every server's models. retry.py: the hook that retries a call that failed
for a temporary reason. The streamed call itself is core's (core/llm.py).
"""

from dataclasses import dataclass

from settings import Setting

# The agent's thinking effort, least to most
MODEL_DEFAULT_EFFORT = "model_default"
THINKING_EFFORTS = (MODEL_DEFAULT_EFFORT, "off", "low", "high", "max")
# The feature of the model's API each effort needs (features.py)
THINKING_EFFORT_FEATURES = {"off": "thinking_off", "low": "thinking_on",
                            "high": "thinking_on", "max": "thinking_on"}
THINKING_EFFORT_NEEDS = tuple(THINKING_EFFORT_FEATURES.items())

# Canonical input/output modalities tracked by passive capability discovery.
MODALITIES = ("text", "image", "audio", "video", "pdf")

# Same-Provider Default routing sentinels shared by utility capability settings.
SERVICE_CURRENT_SERVER = "current_server"
SERVICE_OFF = "off"
SERVICE_CHOICES = (SERVICE_CURRENT_SERVER, SERVICE_OFF)
UTILITY_SETTING_NAMES = (
    "search_service", "stt_service", "tts_service",
    "image_service", "video_service",
)


def _first_known(*values):
    """Return the highest-precedence non-None value, keeping False distinct."""
    for value in values:
        if value is not None:
            return value
    return None


def _tri_and(left, right):
    """Tri-state intersection: False dominates; unknown (None) stays unavailable."""
    if left is False or right is False:
        return False
    if left is True and right is True:
        return True
    return None


def normalize_modalities_list(items):
    """Return known canonical modality names in standard order, or empty list."""
    if not isinstance(items, (list, tuple, set, frozenset)):
        return []
    seen = {
        item.strip().lower() for item in items
        if isinstance(item, str) and item.strip()
    }
    return [name for name in MODALITIES if name in seen]


@dataclass(frozen=True)
class ModalitySupport:
    """Tri-state modality support: True, False (explicit), or None (unknown)."""

    text: bool | None = None
    image: bool | None = None
    audio: bool | None = None
    video: bool | None = None
    pdf: bool | None = None

    def get(self, modality):
        """Raw tri-state value for `modality` (True, False, or None)."""
        if modality not in MODALITIES:
            return None
        return getattr(self, modality)

    def supports(self, modality):
        """Only explicit True is usable; unknown (None) stays unavailable."""
        return self.get(modality) is True

    def known(self):
        """Modalities explicitly known to be supported."""
        return frozenset(name for name in MODALITIES if getattr(self, name) is True)

    def as_dict(self):
        """Only known (True/False) modalities, omitting unknown (None)."""
        return {
            name: value for name in MODALITIES
            if (value := getattr(self, name)) is not None
        }

    @classmethod
    def from_raw(cls, raw):
        """Parse a dict ({modality: bool}) or exhaustive list ([modality, ...])."""
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, dict):
            values = {}
            for name in MODALITIES:
                val = raw.get(name)
                if isinstance(val, bool):
                    values[name] = val
            return cls(**values)
        if isinstance(raw, (list, tuple)):
            known = set(normalize_modalities_list(raw))
            if not known:
                return cls()
            return cls(**{name: (name in known) for name in MODALITIES})
        return cls()

    @classmethod
    def layer(cls, *sources):
        """Resolve tri-state modalities by precedence (first non-None wins)."""
        return cls(**{
            name: _first_known(*(getattr(src, name) for src in sources))
            for name in MODALITIES
        })

    @classmethod
    def intersect(cls, adapter, resolved):
        """Effective support = adapter_declared ∩ resolved."""
        return cls(**{
            name: _tri_and(getattr(adapter, name), getattr(resolved, name))
            for name in MODALITIES
        })


@dataclass(frozen=True)
class ModelCapabilities:
    """Passive capabilities for one (model, endpoint, api, role).

    Tri-state fields preserve explicit False vs None (unknown). Unknown stays
    unavailable with zero inference probes.
    """

    input: ModalitySupport = ModalitySupport()
    output: ModalitySupport = ModalitySupport()
    reasoning: bool | None = None
    tool_calling: bool | None = None
    reply_capable: bool | None = None
    context_window: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    supported_methods: tuple[str, ...] = ()
    role: str = "chat"
    api: str | None = None

    def supports_input(self, modality):
        """True only when input `modality` is explicitly supported."""
        return self.input.supports(modality)

    def supports_output(self, modality):
        """True only when output `modality` is explicitly supported."""
        return self.output.supports(modality)

    def supports_reasoning(self):
        """True only when reasoning is explicitly supported."""
        return self.reasoning is True

    def supports_tool_calling(self):
        """True only when tool calling is explicitly supported."""
        return self.tool_calling is True

    def is_reply_capable(self):
        """False only when explicitly marked non-reply-capable."""
        return self.reply_capable is not False

    def as_dict(self):
        """Serialize known fields for persistence or inspection."""
        data = {}
        if inputs := self.input.as_dict():
            data["input"] = inputs
        if outputs := self.output.as_dict():
            data["output"] = outputs
        if self.reasoning is not None:
            data["reasoning"] = self.reasoning
        if self.tool_calling is not None:
            data["tool_calling"] = self.tool_calling
        if self.reply_capable is not None:
            data["reply_capable"] = self.reply_capable
        if self.context_window is not None:
            data["context_window"] = self.context_window
        if self.max_input_tokens is not None:
            data["max_input_tokens"] = self.max_input_tokens
        if self.max_output_tokens is not None:
            data["max_output_tokens"] = self.max_output_tokens
        if self.supported_methods:
            data["supported_methods"] = list(self.supported_methods)
        return data

    @classmethod
    def from_raw(cls, raw):
        """Parse a capability dict (or return an existing instance)."""
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, dict):
            return cls()
        inputs = ModalitySupport.from_raw(
            raw.get("input", raw.get("input_modalities")),
        )
        outputs = ModalitySupport.from_raw(
            raw.get("output", raw.get("output_modalities")),
        )
        reasoning = raw.get("reasoning") if isinstance(
            raw.get("reasoning"), bool,
        ) else None
        tool_calling = raw.get("tool_calling") if isinstance(
            raw.get("tool_calling"), bool,
        ) else None
        reply_capable = raw.get("reply_capable") if isinstance(
            raw.get("reply_capable"), bool,
        ) else None
        methods = raw.get("supported_methods")
        supported_methods = (
            tuple(m for m in methods if isinstance(m, str))
            if isinstance(methods, (list, tuple)) else ()
        )
        return cls(
            input=inputs,
            output=outputs,
            reasoning=reasoning,
            tool_calling=tool_calling,
            reply_capable=reply_capable,
            supported_methods=supported_methods,
        )

    @classmethod
    def resolve(
        cls,
        adapter,
        *sources,
        context_window=None,
        max_input_tokens=None,
        max_output_tokens=None,
        supported_methods=(),
        role="chat",
        api=None,
    ):
        """Compute effective = adapter_declared ∩ resolved(*sources).

        `sources` are ordered highest-to-lowest precedence:
        user_config > endpoint_metadata > catalog.toml > published_catalog.
        """
        parsed = [cls.from_raw(src) for src in sources if src]
        resolved_input = ModalitySupport.layer(*(s.input for s in parsed))
        resolved_output = ModalitySupport.layer(*(s.output for s in parsed))
        resolved_reasoning = _first_known(*(s.reasoning for s in parsed))
        resolved_tools = _first_known(*(s.tool_calling for s in parsed))
        resolved_reply = _first_known(*(s.reply_capable for s in parsed))

        eff_input = ModalitySupport.intersect(adapter.input, resolved_input)
        eff_output = ModalitySupport.intersect(adapter.output, resolved_output)
        eff_reasoning = _tri_and(adapter.reasoning, resolved_reasoning)
        eff_tools = _tri_and(adapter.tool_calling, resolved_tools)

        # A model explicitly marked non-reply-capable cannot serve chat replies.
        if resolved_reply is False and role == "chat":
            eff_output = ModalitySupport(
                text=False, image=False, audio=False, video=False, pdf=False,
            )
            eff_tools = False

        methods = tuple(supported_methods)
        if not methods:
            for src in parsed:
                if src.supported_methods:
                    methods = src.supported_methods
                    break

        return cls(
            input=eff_input,
            output=eff_output,
            reasoning=eff_reasoning,
            tool_calling=eff_tools,
            reply_capable=resolved_reply,
            context_window=context_window,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            supported_methods=methods,
            role=role,
            api=api or adapter.api,
        )


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
    Setting("thinking_effort", "Default", "Thinking effort", MODEL_DEFAULT_EFFORT,
            "choice", choices=THINKING_EFFORTS, needs=THINKING_EFFORT_NEEDS,
            runs_on="model", aliases=("effort",), switchable=True,
            model_visible=True),
    # Utility capability routing under Same-Provider Default.
    Setting("stt_service", "Default", "Speech-to-text service",
            SERVICE_CURRENT_SERVER, "service", choices=SERVICE_CHOICES,
            switchable=True, none_means_default=True),
    Setting("tts_service", "Default", "Text-to-speech service",
            SERVICE_CURRENT_SERVER, "service", choices=SERVICE_CHOICES,
            switchable=True, none_means_default=True),
    Setting("image_service", "Default", "Image generation service",
            SERVICE_CURRENT_SERVER, "service", choices=SERVICE_CHOICES,
            switchable=True, none_means_default=True),
    Setting("video_service", "Default", "Video generation service",
            SERVICE_CURRENT_SERVER, "service", choices=SERVICE_CHOICES,
            switchable=True, none_means_default=True),
    # Silence, not the whole call: before the first token and between
    # tokens, a server's progress through the prompt counting as life where
    # the model honours it (features.py). A backstop for a server stuck
    # silent: Codex waits 300 s, opencode 900, Hermes 180 and 900 for a
    # local server; 45 s failed
    # a local server's healthy reading of 143k tokens
    Setting("model_silence_seconds", "Limits", "Seconds the model may stay silent", 300,
            "number", aliases=("model_timeout",)),
    Setting("model_retries", "Limits", "Retries after a model error", 3, "int",
            minimum=0),
    # About this, twice that, four times that between retries
    Setting("model_retry_seconds", "Limits", "Seconds before the first retry", 1.0,
            "number", aliases=("retry_base",)),
)
