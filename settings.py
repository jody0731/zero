"""The settings engine: what config.toml may set, each setting checked, read
and written.

Each setting is declared once, by the part it belongs to, and the assembly
adds them all here when ZERO starts (`add`), in the order the settings view
lists them. The harness writes no default of its own: the assembly hands
each part its values from here.

config.toml sits in ZERO's config folder (paths.py). Its top-level settings
are ZERO's, with one writer, the server (the settings view sends each change
through it, `Config.set`); hand edits work too. A table in it is a face's own
(the full screen's [tui]): this module skips it and keeps it as it is.
"""

import json
import os
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

import paths

@dataclass(frozen=True)
class Setting:
    """One setting: its group and label in the settings view (/settings), what
    holds without it, and what it takes. `kind`: "choice" (one of `choices`),
    "mode", "model", "int" (a whole number, `minimum` or more) or "number" (above
    0). `unset_means`: what None means, where a setting can be left unset. `shown`:
    False for a setting the view leaves out (config.toml may still set it).
    `needs`: (value, feature) pairs, the feature of its model's API each value
    needs (model/features.py); `runs_on`: the setting that names that model,
    the session's model when it is unset. A value whose feature the model
    doesn't honour shows unavailable there, and is kept but not sent."""

    name: str
    group: str
    label: str
    default: object
    kind: str
    choices: tuple = ()
    minimum: int = 1
    unset_means: str | None = None
    shown: bool = True
    needs: tuple = ()
    runs_on: str | None = None


# Every setting ZERO knows, in the settings view's order (add); by name; what
# holds without config.toml (model None: the model's default, model/); what
# the view lists. The "Limits" group is a session's limits: it keeps the ones
# it was made with
SETTINGS = []
BY_NAME = {}
DEFAULTS = {}
SHOWN = []
# The settings view's order, by name, as the assembly gives it; the rest after
ORDER = []


def add(*declared, order=None):
    """Know these settings from now on. `order`: the names in the settings
    view's order, kept for every later add; a setting it doesn't name comes
    after, in the order added. Adding the same setting again changes
    nothing; another one by a known name is an error."""
    for setting in declared:
        if BY_NAME.get(setting.name) is setting:
            continue
        if setting.name in BY_NAME:
            raise ValueError(f"setting {setting.name!r} declared twice")
        SETTINGS.append(setting)
        BY_NAME[setting.name] = setting
    if order is not None:
        ORDER[:] = order
    place = {name: i for i, name in enumerate(ORDER)}
    SETTINGS.sort(key=lambda s: place.get(s.name, len(place)))
    BY_NAME.clear()
    DEFAULTS.clear()
    for setting in SETTINGS:
        BY_NAME[setting.name] = setting
        DEFAULTS[setting.name] = setting.default
    SHOWN[:] = [s for s in SETTINGS if s.shown]


def default_limits():
    """A session's limits at their defaults: the "Limits" group, by name."""
    return {s.name: s.default for s in SETTINGS if s.group == "Limits"}


HEADER = (
    "# ZERO's settings. The settings view (/settings in zero) writes this file\n"
    "# and leaves out what is at its default; edits by hand work too.\n"
)


class SettingsError(Exception):
    """config.toml is broken: none of it is taken, rather than half of it."""


def check(setting, value, mode=None):
    """What is wrong with `value` for `setting` (a Setting, or its name);
    None if nothing. `mode`: the server's check of a mode's name, which raises
    ValueError; this module knows no modes of its own."""
    if not isinstance(setting, Setting):
        setting = BY_NAME[setting]
    name = setting.name
    if value is None:
        return None if setting.unset_means else f"{name} needs a value"
    # A bool is an int in Python, and never a count of steps or minutes
    if isinstance(value, bool):
        return f"{name} has the wrong type"
    if setting.kind in ("choice", "mode", "model"):
        if not isinstance(value, str) or not value.strip():
            return f"{name} has the wrong type"
        if setting.kind == "choice" and value not in setting.choices:
            return f"{name} is one of {', '.join(setting.choices)}"
        if setting.kind == "mode" and mode is not None:
            try:
                mode(value)
            except ValueError:
                return f"{name}: unknown mode {value!r}"
        return None
    if setting.kind == "int":
        if not isinstance(value, int):
            return f"{name} has the wrong type"
        if value < setting.minimum:
            return f"{name} must be {setting.minimum} or more"
        return None
    if not isinstance(value, (int, float)):
        return f"{name} has the wrong type"
    if value <= 0:
        return f"{name} must be above 0"
    return None


def load(path=None, mode=None):
    """config.toml's settings over the defaults; no file, the defaults.
    `mode`: as for check. Its tables are the faces' and left out."""
    path = Path(path) if path else paths.config_file()
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return dict(DEFAULTS)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SettingsError(f"can't read {path}: {e}") from e
    data = {name: value for name, value in data.items()
            if not isinstance(value, dict)}
    data = renamed(data)
    unknown = set(data) - set(DEFAULTS)
    if unknown:
        raise SettingsError(
            f"{path}: unknown settings {sorted(unknown)}; known: {sorted(DEFAULTS)}"
        )
    for name, value in data.items():
        problem = check(name, value, mode)
        if problem:
            raise SettingsError(f"{path}: {problem}")
    return {**DEFAULTS, **data}


# Settings named otherwise before 0.6.2.8, still read under their old names;
# the next save writes the new ones
RENAMED = {
    "effort": "thinking_effort", "judge_effort": "judge_thinking_effort",
    "idle_minutes": "server_idle_minutes", "max_format_errors": "max_broken_replies",
    "output_limit": "bash_output_chars", "web_chars": "web_fetch_chars",
    "web_results": "web_search_results", "retry_base": "model_retry_seconds",
    "step_grant": "extra_steps", "bash_timeout": "bash_foreground_seconds",
    "judge_timeout": "judge_silence_seconds", "model_timeout": "model_silence_seconds",
    "consent_timeout": "consent_silence_seconds",
}


def renamed(values):
    """`values` under the settings' names as they are now; a new name beats
    its old one where a file has both."""
    out = {RENAMED.get(name, name): value for name, value in values.items()
           if name in RENAMED}
    out.update({name: value for name, value in values.items()
                if name not in RENAMED})
    return out


def save(values, path=None):
    """Write `values` to config.toml, leaving out what is at its default, and
    the file's tables as they are; in one move, so a reader never sees half a
    file."""
    path = Path(path) if path else paths.config_file()
    lines = [HEADER]
    for setting in SETTINGS:
        value = values.get(setting.name, setting.default)
        if value is not None and value != setting.default:
            lines.append(f"{setting.name} = {toml(value)}\n")
    tables = "".join(text for header, text in sections(read(path)) if header)
    if tables:
        lines.append("\n" + tables.lstrip("\n"))
    write(path, "".join(lines))


def read(path):
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        return ""


def write(path, text):
    """The whole file in one move."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    draft = path.with_name(path.name + ".new")
    draft.write_text(text)
    os.replace(draft, path)


def sections(text):
    """A TOML file's text in pieces, each with its table's header line: the
    top-level keys first (header None), then each table."""
    pieces, header, lines = [], None, []
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("["):
            pieces.append((header, "".join(lines)))
            header, lines = line.strip(), []
        lines.append(line)
    pieces.append((header, "".join(lines)))
    return pieces


def toml(value):
    if isinstance(value, str):
        # A JSON string is a TOML basic string
        return json.dumps(value, ensure_ascii=False)
    return repr(value)


def stamp(path):
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


class Config:
    """config.toml as it stands, read again whenever the file changes, for
    the server: new sessions, the idle time, the settings view. A broken edit
    keeps the settings it had, and `error` says what is wrong; a file broken
    at the first read starts the server on the defaults, the error said."""

    def __init__(self, path=None, mode=None):
        self.path = Path(path) if path else paths.config_file()
        self.mode = mode
        self.stamp = stamp(self.path)
        self.error = None
        try:
            self.values = load(self.path, mode)
        except SettingsError as e:
            self.values = dict(DEFAULTS)
            self.error = str(e)
            print(f"ZERO's server: {e}; starting on the defaults", flush=True)

    def __call__(self):
        now = stamp(self.path)
        if now != self.stamp:
            self.stamp = now
            try:
                self.values = load(self.path, self.mode)
                self.error = None
            except SettingsError as e:
                self.error = str(e)
                print(f"ZERO's server: {e}; keeping the settings it had", flush=True)
        return self.values

    def listing(self):
        """Every setting with its value, for the settings view: its name,
        group, label, default, kind, choices, minimum, unset_means, shown, needs and
        runs_on."""
        values = self()
        return [{**asdict(s), "choices": list(s.choices),
                 "needs": [list(pair) for pair in s.needs],
                 "value": values[s.name]}
                for s in SETTINGS]

    def set(self, name, value):
        """Write one setting; what is wrong with it, or None once written."""
        if name not in BY_NAME:
            return f"no setting {name!r}"
        self()
        if self.error is not None:
            return "config.toml can't be read: fix it first"
        problem = check(name, value, self.mode)
        if problem:
            return problem
        save({**self.values, name: value}, self.path)
        self.stamp = None
        self()
        return None

