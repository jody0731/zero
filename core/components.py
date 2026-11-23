"""Component manifest and build-context carriers."""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ComponentManifest:
    """Everything a component declares to the assembly once."""

    name: str
    order: int = 0
    entries: tuple = ()
    settings: tuple = ()
    system_sources: tuple[str, ...] = ()
    prepare_session: object = None
    session_hooks: object = None
    after_session: object = None
    service: object = None
    doctor: object = None


@dataclass(slots=True)
class SessionBuild:
    """Shared build context passed through component prepare, hook, and after steps."""

    model: str
    mode: str
    runs_as: str
    server: object
    resolved: object
    limits: object
    log: object
    workspace: object
    ask: object
    options: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    tool_context: dict = field(default_factory=dict)
    hooks: list = field(default_factory=list)


@dataclass(slots=True)
class ServiceBuild:
    """Shared build context passed to component service factories."""

    config: object
    limits: object
    options: dict = field(default_factory=dict)
    services: dict = field(default_factory=dict)
