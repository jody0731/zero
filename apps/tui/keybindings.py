"""The CLI's keys: each action's id, its default keys, where it works.

config.toml's [keys] table maps an action's id to keys that replace its
defaults, written by the key bindings view (/settings, then Key bindings). The
bindings take them through Textual's keymap (a Binding's id); the input's own
keys (send, new line) are looked up here. Keys are Textual's names, several
joined by commas: "ctrl+o", "ctrl+j,alt+enter".
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Action:
    keys: str
    what: str
    # Where it works: "input" (the message box), "session" (its view),
    # "sessions" (the list), "app" (everywhere)
    where: str


ACTIONS = {
    "send": Action("enter", "Send the message", "input"),
    "new_line": Action("shift+enter,ctrl+enter", "Start a new line", "input"),
    # Alt+Enter reaches the app only where the terminal reports it (the kitty
    # protocol); Ctrl+J always does
    "follow_up": Action("ctrl+j,alt+enter", "Send it to run after the answer",
                        "app"),
    "cycle_mode": Action("shift+tab", "Next mode: default, auto, auto+, readOnly",
                         "session"),
    "switch_view": Action("tab", "Sessions view and back (completes a / command)",
                          "app"),
    "sessions": Action("ctrl+o", "Sessions view; in it, the archived ones", "app"),
    "next_needing_you": Action("alt+down", "Next session that needs you", "app"),
    "previous_session": Action("alt+up", "The session before", "app"),
    "page_up": Action("pageup", "Scroll the transcript up", "session"),
    "page_down": Action("pagedown", "Scroll the transcript down", "session"),
    "to_end": Action("ctrl+end", "Go to the end and follow it", "session"),
    "toggle_folds": Action("ctrl+t", "Open or close every command's output",
                           "session"),
    "interrupt": Action(
        "ctrl+c", "Copy the selection, else stop the turn or clear the input",
        "app",
    ),
    "leave": Action("ctrl+d", "Leave", "app"),
    "rename": Action("ctrl+r", "Rename the chosen session", "sessions"),
    "archive": Action("ctrl+a", "Archive or unarchive it", "sessions"),
    "trash": Action("delete", "Move it to the trash", "sessions"),
    "new_elsewhere": Action("ctrl+n", "New session in another folder",
                            "sessions"),
}
# Actions named otherwise before 0.6.2.8, read under their names now
RENAMED = {"elsewhere": "new_elsewhere", "folds": "toggle_folds",
           "next_needing": "next_needing_you", "go_back": "previous_session"}
# The key bindings view's groups, in order
PLACES = {
    "input": "Writing",
    "session": "The session view",
    "app": "Everywhere",
    "sessions": "The sessions view",
}
# Keys the views keep for themselves: Esc goes back or cancels a capture; the
# arrows, Home, End and Backspace move in the lists and edit the input
RESERVED_KEYS = ("escape", "up", "down", "left", "right", "home", "end", "backspace")
# What each place's keys can't share a key with
OVERLAPS = {
    "input": ("input", "session", "app"),
    "session": ("input", "session", "app"),
    "sessions": ("sessions", "app"),
    "app": ("input", "session", "sessions", "app"),
}
NAMES = {"pageup": "PgUp", "pagedown": "PgDn", "delete": "Del", "escape": "Esc"}


def split(keys):
    return tuple(key.strip() for key in keys.split(",") if key.strip())


def current_keys(overrides=None):
    """Each action's keys as a tuple: the user's where set, else the defaults."""
    overrides = overrides or {}
    return {
        id: split(overrides.get(id) or action.keys)
        for id, action in ACTIONS.items()
    }


def keymap(overrides=None):
    """Textual's keymap: the bindings' ids the user changed."""
    return {
        id: keys for id, keys in (overrides or {}).items()
        if id in ACTIONS and ACTIONS[id].where != "input" and keys
    }


def clashes(id, key, overrides=None):
    """The other actions that `key` already does where `id` works."""
    places = OVERLAPS[ACTIONS[id].where]
    return [
        other for other, keys in current_keys(overrides).items()
        if other != id and key in keys and ACTIONS[other].where in places
    ]


def pretty(key):
    """ctrl+o → Ctrl+O, shift+enter → Shift+Enter, pageup → PgUp."""
    *mods, last = key.split("+")
    last = NAMES.get(last) or (last.upper() if len(last) == 1 else last.title())
    return "+".join([*(mod.title() for mod in mods), last])


def shown(keys):
    return ", ".join(pretty(key) for key in keys)


def first_key(id, overrides=None):
    """The action's first key, as the views' hints name it."""
    return pretty(current_keys(overrides)[id][0])
