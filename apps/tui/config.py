"""The full screen's own settings: the [tui] table of config.toml (paths.py),
with its theme, the mouse, and the key bindings ([tui.keys], keybindings.py).

The full screen writes this table and keeps the rest of the file as it is:
ZERO's settings above it are the server's to write (settings.py). Before
0.6.2.8 the table was [cli], read while there is no [tui], and written as
[tui] at the next save; an action's old id reads as its new one
(keybindings.RENAMED). Before the table, theme, mouse and [keys] sat at the
file's top level; the first start moves them under it (`move`).
"""

import tomllib
from pathlib import Path

import paths
import settings
from apps.tui import keybindings
from settings import Setting, SettingsError

TABLE = "tui"
# The table's name before 0.6.2.8
OLD_TABLE = "cli"
# Textual's own themes (8.2), offered after ZERO's auto, dark and light; named
# here, so a test can check the list against Textual's
TEXTUAL_THEMES = (
    "textual-dark", "textual-light", "ansi-dark", "ansi-light", "atom-one-dark",
    "atom-one-light", "catppuccin-frappe", "catppuccin-latte",
    "catppuccin-macchiato", "catppuccin-mocha", "dracula", "flexoki", "gruvbox",
    "monokai", "nord", "rose-pine", "rose-pine-dawn", "rose-pine-moon",
    "solarized-dark", "solarized-light", "tokyo-night",
)
SETTINGS = (
    Setting("theme", "TUI", "Theme", "auto", "choice",
            choices=("auto", "dark", "light", *TEXTUAL_THEMES)),
    # On (as Claude Code's and Codex's full screens): ZERO selects and
    # Ctrl+C copies, never on release; Shift+drag is the terminal's own
    # selection, and a click on a detached widget is dropped (Textual #6643).
    # Off: the terminal selects and no click reaches Textual. Read when zero
    # opens
    Setting("mouse", "TUI", "The mouse in ZERO (off: the terminal selects)",
            "on", "choice", choices=("off", "on")),
)
BY_NAME = {setting.name: setting for setting in SETTINGS}
# keys: an action's id → its keys, where the user changed them
DEFAULTS = {**{s.name: s.default for s in SETTINGS}, "keys": {}}


def load(path=None):
    """The [tui] table (else the [cli] of before) over the defaults; no file
    or no table, the defaults. SettingsError for a file that can't be read
    or a wrong value."""
    path = Path(path) if path else paths.config_file()
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {**DEFAULTS, "keys": {}}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SettingsError(f"can't read {path}: {e}") from e
    name = TABLE if TABLE in data or OLD_TABLE not in data else OLD_TABLE
    table = data.get(name, {})
    if not isinstance(table, dict):
        raise SettingsError(f"{path}: [{name}] is a table")
    unknown = set(table) - set(DEFAULTS)
    if unknown:
        raise SettingsError(
            f"{path}: unknown settings in [{name}] {sorted(unknown)}; known: "
            f"{sorted(DEFAULTS)}"
        )
    keys = table.get("keys", {})
    if not isinstance(keys, dict) or not all(
        isinstance(v, str) for v in keys.values()
    ):
        raise SettingsError(
            f"{path}: [{name}.keys] maps an action to its keys, a string"
        )
    for name, value in table.items():
        problem = None if name == "keys" else settings.check(BY_NAME[name], value)
        if problem:
            raise SettingsError(f"{path}: {problem}")
    return {**DEFAULTS, **table, "keys": renamed(keys)}


def renamed(keys):
    """The keys under the actions' ids now; an id beats its old one."""
    found = {keybindings.RENAMED.get(id, id): value
             for id, value in keys.items() if id not in keybindings.RENAMED}
    for old, new in keybindings.RENAMED.items():
        if old in keys and new not in keys:
            found[new] = keys[old]
    return found


def save(values, path=None):
    """Write the [tui] table, leaving out what is at its default, and the
    rest of the file as it is (a [cli] of before goes)."""
    path = Path(path) if path else paths.config_file()
    kept = [text for header, text in settings.sections(settings.read(path))
            if not in_tui_table(header)]
    write(path, kept, table(values))


def in_tui_table(header):
    """Whether a section's header line opens this table, or one inside it,
    under its name now or before."""
    if header is None:
        return False
    name = header.strip("[]").strip()
    return any(name == table or name.startswith(table + ".")
               for table in (TABLE, OLD_TABLE))


def table(values):
    """The [tui] table's text: its settings, then its keys."""
    lines = []
    for setting in SETTINGS:
        value = values.get(setting.name, setting.default)
        if value != setting.default:
            lines.append(f"{setting.name} = {settings.toml(value)}\n")
    text = f"[{TABLE}]\n" + "".join(lines) if lines else ""
    keys = values.get("keys") or {}
    if keys:
        text += "\n" if text else ""
        text += f"[{TABLE}.keys]\n" + "".join(
            f"{name} = {settings.toml(keys[name])}\n" for name in sorted(keys)
        )
    return text


def write(path, kept, text):
    """The kept sections, then the table, in one move."""
    body = "".join(kept).rstrip("\n")
    if text:
        body = f"{body}\n\n{text}" if body else text
    elif body:
        body += "\n"
    settings.write(path, body)


# Before the table: these sat at the file's top level
TOP_LEVEL_KEYS = ("theme", "mouse")


def move(path=None):
    """Move theme, mouse and [keys] from the file's top level under [tui],
    once; whether anything moved. A file that can't be read is left alone."""
    path = Path(path) if path else paths.config_file()
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return False
    old = {name: data[name] for name in TOP_LEVEL_KEYS if name in data}
    keys = data.get("keys")
    if not old and not isinstance(keys, dict):
        return False
    name = TABLE if TABLE in data else OLD_TABLE
    current = data.get(name) if isinstance(data.get(name), dict) else {}
    values = {**DEFAULTS, **old, **current,
              "keys": renamed({**(keys or {}), **current.get("keys", {})})}
    kept = []
    for header, text in settings.sections(path.read_text()):
        if header is None:
            text = "".join(
                line for line in text.splitlines(keepends=True)
                if line.split("=", 1)[0].strip() not in TOP_LEVEL_KEYS
            )
        elif header == "[keys]" or in_tui_table(header):
            continue
        kept.append(text)
    write(path, kept, table(values))
    return True
