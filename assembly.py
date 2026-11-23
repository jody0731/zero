"""The assembly: declarative component discovery and session/service composition.

Each component declares its manifest once (`components/*/manifest.py`), and the
assembly discovers every manifest in `(manifest.order, manifest.name)` order
without hardcoding component tables. Importing a component registers nothing
(design rule 3); importing this module fills the core registries (`entries`,
`settings`, `timeline`, `tools`, `attribution`) once from the discovered
manifests.
"""

import hashlib
import importlib
import json
from pathlib import Path

import settings
from core import attribution, entries, timeline, tools
from core.asking import PARK
from core.components import ComponentManifest, ServiceBuild, SessionBuild
from core.loop import Session
from server import SETTINGS as SERVER_SETTINGS
from sessions.entries import ENTRY_SPECS as SESSION_ENTRIES
from sessions.log import Log, fresh_log
from settings import LiveSettings

HERE = Path(__file__).resolve().parent


def _discover(root=HERE):
    """Discover `(manifest, module, folder, package)` sorted by `(order, name)`."""
    parent = Path(root) / "components"
    found = []
    if parent.is_dir():
        for path in sorted(parent.glob("*/manifest.py")):
            folder = path.parent
            if folder.name.startswith((".", "_")):
                continue
            package = f"components.{folder.name}"
            module = importlib.import_module(f"{package}.manifest")
            manifest = getattr(module, "MANIFEST", None)
            if not isinstance(manifest, ComponentManifest):
                raise TypeError(f"{path} must export MANIFEST: ComponentManifest")
            found.append((manifest, module, folder, package))
    found.sort(key=lambda item: (item[0].order, item[0].name))
    return tuple(found)


_DISCOVERED = _discover()
MANIFESTS = tuple(manifest for manifest, _, _, _ in _DISCOVERED)

entries.add(
    *SESSION_ENTRIES,
    *(spec for manifest in MANIFESTS for spec in manifest.entries),
)
settings.add(
    *(setting for manifest in MANIFESTS for setting in manifest.settings),
    *SERVER_SETTINGS,
)
for _setting in settings.SETTINGS:
    if _setting.switchable:
        timeline.add_switchable(
            _setting.name, _setting.aliases, model_visible=_setting.model_visible,
        )

for _manifest, _module, _folder, _package in _DISCOVERED:
    _register = getattr(_module, "register", None)
    if callable(_register):
        _register()
    if (_folder / "tool.py").is_file():
        _tool_mod = importlib.import_module(f"{_package}.tool")
        _declared_tools = getattr(_tool_mod, "TOOLS", ())
        if _declared_tools:
            tools.add(*_declared_tools)
        _senders = getattr(_tool_mod, "NOTICE_SENDERS", None)
        if _senders:
            attribution.add_notice_senders(_senders)

DEFAULT_LIMITS = settings.default_limits()
SYSTEM_MESSAGE_SOURCES = tuple(
    (folder / source).relative_to(HERE).as_posix()
    for manifest, _, folder, _ in _DISCOVERED
    for source in manifest.system_sources
)


def _manifest_attr(name):
    """Return the first attribute named `name` exported by a discovered manifest."""
    for _, module, _, _ in _DISCOVERED:
        if hasattr(module, name):
            return getattr(module, name)
    return None


DEFAULT_ASK = _manifest_attr("ask_user")


def mode_rows():
    """Return the declared permission mode rows for apps and the server."""
    rows = _manifest_attr("mode_rows")
    return rows() if callable(rows) else []


def doctor_lines(workspace=None):
    """Run every component manifest's doctor check in manifest order."""
    lines = []
    for manifest in MANIFESTS:
        if manifest.doctor is not None:
            lines.extend(manifest.doctor(workspace))
    return lines


def code_mark(session):
    """What the model reads of the code, as a short hash: the session's tools
    and the component sources that shape its system message."""
    digest = hashlib.sha256(
        json.dumps(session.tool_schemas(), sort_keys=True).encode()
    )
    for name in SYSTEM_MESSAGE_SOURCES:
        digest.update((HERE / name).read_bytes())
    # Who sends notices, which the system message names.
    digest.update(json.dumps(attribution.NOTICES, sort_keys=True).encode())
    for contributor in session.tool_context.get("code_mark_parts", ()):
        digest.update(contributor(session))
    return digest.hexdigest()[:12]


def make_session(
    model,
    folder=None,
    mode="default",
    ask=DEFAULT_ASK,
    limits=None,
    workspace=None,
    log=None,
    **options,
):
    """Build a session by running component prepare, hook, and after phases."""
    if workspace is None:
        raise ValueError("a session needs a workspace")
    if not isinstance(limits, LiveSettings):
        limits = LiveSettings(fixed=limits)
    if log is None and folder is None:
        raise ValueError("a session needs its log, or a folder to start one in")
    session_log = log if log is not None else fresh_log(folder, model)
    initial_settings = dict(options.pop("_initial_settings", {}))
    for setting in settings.SETTINGS:
        if setting.switchable and setting.name in options:
            initial_settings[setting.name] = options[setting.name]
    build = SessionBuild(
        model=model,
        mode=mode,
        runs_as=mode,
        server=None,
        resolved=None,
        limits=limits,
        log=session_log,
        workspace=workspace,
        ask=ask,
        options=dict(options),
        settings=initial_settings,
        tool_context={"limits": limits},
    )
    for manifest in MANIFESTS:
        if manifest.prepare_session is not None:
            manifest.prepare_session(build)
    for manifest in MANIFESTS:
        if manifest.session_hooks is not None:
            build.hooks.extend(manifest.session_hooks(build))
    session = Session(
        build.state["llm"],
        build.hooks,
        build.state.get("system_prompt", ""),
        session_log,
        build.settings,
        tool_context=build.tool_context,
        rebuild=build.state.get("rebuild_system_prompt"),
        code_mark=lambda: code_mark(session),
        fallback=build.state.get("fallback"),
    )
    for manifest in MANIFESTS:
        if manifest.after_session is not None:
            manifest.after_session(session, build)
    return session


def _logged_switches(log):
    head = log.first_record()
    if head is None or not entries.has(head.entry, "header"):
        return {}
    return log.settings_now()


def server_session(paths, sid, info, log=None, ask=PARK, config=None, **options):
    """Build a server-hosted session with live limits and logged switches."""
    log = log if log is not None else Log(paths.sessions / f"{sid}.jsonl")
    config = config or (lambda: settings.DEFAULTS)
    now = config()
    logged = _logged_switches(log)
    initial_settings = {}
    for setting in settings.SETTINGS:
        if not setting.switchable:
            continue
        if setting.name in options:
            initial_settings[setting.name] = options[setting.name]
        elif setting.name in info and (
            info[setting.name] is not None or setting.unset_means is not None
        ):
            initial_settings[setting.name] = info[setting.name]
        elif setting.name in logged:
            initial_settings[setting.name] = logged[setting.name]
        else:
            initial_settings[setting.name] = now.get(setting.name, setting.default)
    limits = LiveSettings(config, fixed=options.pop("limits", {}))
    merged = {
        "workspace": Path(info["workspace"]),
        "unattended": info.get("started_by") == "zero",
        "on_a_server": True,
        "_info": info,
        "_initial_settings": initial_settings,
        **options,
    }
    session = make_session(
        info["model"],
        log=log,
        mode=info["mode"],
        ask=ask,
        limits=limits,
        **merged,
    )
    # A server carries it: notices and messages reach its running turn.
    session.tool_context["on_a_server"] = True
    return session


def server_session_maker(paths, config, limits=None):
    """How the server makes a session over `config()`, with optional `limits`."""
    return ServerSessionMaker(paths, config, {"limits": limits} if limits else {})


class ServerSessionMaker:
    """Session maker that keeps each session's scratch folder while running."""

    def __init__(self, paths, config, options):
        self.paths = paths
        self.config = config
        self.options = options
        self.scratches = {}

    def __call__(self, sid, info, log=None, ask=PARK):
        return server_session(
            self.paths,
            sid,
            info,
            log,
            ask,
            config=self.config,
            scratch=self.scratch_for(sid),
            **self.options,
        )

    def scratch_for(self, sid):
        """The session's scratch folder, marked with this server's home."""
        if sid not in self.scratches:
            make_scratch = _manifest_attr("make_scratch")
            self.scratches[sid] = (
                make_scratch(self.paths.home) if callable(make_scratch) else None
            )
        return self.scratches[sid]

    def forget(self, sid):
        """Release a deleted session's scratch folder."""
        self.scratches.pop(sid, None)


def services(config, **options):
    """Instantiate every component service declared in the discovered manifests."""
    build = ServiceBuild(
        config=config,
        limits=LiveSettings(config),
        options=dict(options),
    )
    created = []
    for manifest in MANIFESTS:
        if manifest.service is None:
            continue
        service = manifest.service(build)
        if service is None:
            continue
        created.append(service)
        build.services[service.name] = service
        build.services.setdefault(manifest.name, service)
    return created
