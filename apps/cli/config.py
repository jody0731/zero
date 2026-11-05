"""The CLI's own settings: the [cli] table of config.toml (paths.py), with its
theme, the mouse, and the key bindings ([cli.keys], keybindings.py).

The CLI writes this table and keeps the rest of the file as it is: ZERO's
settings above it are the host's to write (settings.py). Before the table,
theme, mouse and [keys] sat at the file's top level; the first start of the
CLI moves them under it (`move`).
"""

import tomllib
from pathlib import Path

import paths
import settings
from settings import Setting, SettingsError

TABLE = "cli"
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
    Setting("theme", "Look", "Theme", "auto", "choice",
            choices=("auto", "dark", "light", *TEXTUAL_THEMES)),
    # On (as Claude Code's and Codex's full screens): ZERO selects and
    # Ctrl+C copies, never on release; Shift+drag is the terminal's own
    # selection, and a click on a detached widget is dropped (Textual #6643).
    # Off: the terminal selects and no click reaches Textual. Read when zero
    # opens
    Setting("mouse", "Look", "The mouse in ZERO (off: the terminal selects)",
            "on", "choice", choices=("off", "on")),
)
BY_NAME = {setting.name: setting for setting in SETTINGS}
# keys: an action's id → its keys, where the user changed them
DEFAULTS = {**{s.name: s.default for s in SETTINGS}, "keys": {}}


def load(path=None):
    """The [cli] table over the defaults; no file or no table, the
    defaults. SettingsError for a file that can't be read or a wrong value."""
    path = Path(path) if path else paths.config_file()
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {**DEFAULTS, "keys": {}}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SettingsError(f"can't read {path}: {e}") from e
    table = data.get(TABLE, {})
    if not isinstance(table, dict):
        raise SettingsError(f"{path}: [{TABLE}] is a table")
    unknown = set(table) - set(DEFAULTS)
    if unknown:
        raise SettingsError(
            f"{path}: unknown settings in [{TABLE}] {sorted(unknown)}; known: "
            f"{sorted(DEFAULTS)}"
        )
    keys = table.get("keys", {})
    if not isinstance(keys, dict) or not all(
        isinstance(v, str) for v in keys.values()
    ):
        raise SettingsError(
            f"{path}: [{TABLE}.keys] maps an action to its keys, a string"
        )
    for name, value in table.items():
        problem = None if name == "keys" else settings.check(BY_NAME[name], value)
        if problem:
            raise SettingsError(f"{path}: {problem}")
    return {**DEFAULTS, **table, "keys": dict(keys)}


def save(values, path=None):
    """Write the [cli] table, leaving out what is at its default, and the
    rest of the file as it is."""
    path = Path(path) if path else paths.config_file()
    kept = [text for header, text in settings.sections(settings.read(path))
            if not mine(header)]
    write(path, kept, table(values))


def mine(header):
    """Whether a section's header line opens this table or one inside it."""
    if header is None:
        return False
    name = header.strip("[]").strip()
    return name == TABLE or name.startswith(TABLE + ".")


def table(values):
    """The [cli] table's text: its settings, then its keys."""
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


# Before the [cli] table: these sat at the file's top level
OLD = ("theme", "mouse")


def move(path=None):
    """Move theme, mouse and [keys] from the file's top level under [cli],
    once; whether anything moved. A file that can't be read is left alone."""
    path = Path(path) if path else paths.config_file()
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return False
    old = {name: data[name] for name in OLD if name in data}
    keys = data.get("keys")
    if not old and not isinstance(keys, dict):
        return False
    current = data.get(TABLE) if isinstance(data.get(TABLE), dict) else {}
    values = {**DEFAULTS, **old, **current,
              "keys": {**(keys or {}), **current.get("keys", {})}}
    kept = []
    for header, text in settings.sections(path.read_text()):
        if header is None:
            text = "".join(
                line for line in text.splitlines(keepends=True)
                if line.split("=", 1)[0].strip() not in OLD
            )
        elif header == "[keys]" or mine(header):
            continue
        kept.append(text)
    write(path, kept, table(values))
    return True
