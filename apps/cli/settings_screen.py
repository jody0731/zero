"""The settings view, through the host's `settings` and `set_setting` verbs
(ZERO's) and config.py (the CLI's own [cli] table), and the key
bindings view."""

from rich.text import Text
from textual.binding import Binding
from textual.content import Content
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

import paths
import settings
from apps.cli import config, controller, keybindings
from apps.cli.dialogs import CaptureView, ChoiceView, PromptView
from apps.cli.widgets import ERRORS, Plain
from client import session_list


class SettingsView(Screen):
    """config.toml's settings, a row each, grouped, and a row that opens the
    key bindings: the CLI's own first ([cli], config.py), then
    ZERO's, which the host lists and writes. Each change is written at
    once."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", show=False),
        Binding("up", "move(-1)", show=False, priority=True),
        Binding("down", "move(1)", show=False, priority=True),
        Binding("enter", "change", show=False, priority=True),
        Binding("delete,backspace", "reset", show=False, priority=True),
    ]

    def __init__(self, zero):
        super().__init__()
        self.zero = zero
        self.selected = 0
        # ZERO's settings as the host has them: name → value, and what is
        # wrong with config.toml, if anything
        self.values = {}
        self.error = None
        # Each setting, then the key bindings (None)
        self.rows = [*config.SETTINGS, None]

    def fetch(self):
        """ZERO's settings and their values, from the host."""
        try:
            listed, self.error = self.zero.connection.settings()
        except ERRORS as error:
            self.notify(str(error), severity="error")
            return
        shown = []
        for row in listed:
            fields = {k: v for k, v in row.items() if k != "value"}
            setting = settings.Setting(**{**fields, "choices": tuple(row["choices"])})
            self.values[setting.name] = row["value"]
            if setting.shown:
                shown.append(setting)
        self.rows = [*config.SETTINGS, *shown, None]

    def value(self, setting):
        if setting.name in config.BY_NAME:
            return self.zero.config[setting.name]
        return self.values[setting.name]

    def problem(self):
        """What is wrong with config.toml, as the CLI or the host read it."""
        return self.zero.config_error or self.error

    def compose(self):
        where = session_list.short(str(paths.config_file()))
        yield Plain(f"Settings · {where}", classes="view-title")
        yield VerticalScroll(Plain(id="rows"))
        yield Plain(id="preview")
        yield Plain("↑↓ choose · Enter change · Del back to the default · "
                     "Esc back", classes="view-keys")

    def on_mount(self):
        self.fetch()
        self.draw()

    def on_screen_resume(self):
        self.draw()

    def draw(self):
        mine = self.zero.config
        self.selected = max(0, min(self.selected, len(self.rows) - 1))
        text = Text()
        group = None
        for index, setting in enumerate(self.rows):
            heading = setting.group if setting else "Keys"
            if heading != group:
                group = heading
                text.append(f"\n{heading}\n", style="dim")
            style = "reverse" if index == self.selected else ""
            if setting is None:
                label, changed = "Key bindings…", bool(mine["keys"])
                value = f"{len(mine['keys'])} changed" if changed else "defaults"
            else:
                label = setting.label
                now = self.value(setting)
                changed = now != setting.default
                value = controller.setting_text(setting, now, self.zero.default_model())
            text.append(f"{label:<50} ", style=style)
            text.append(value, style=f"{style} bold" if changed else style)
            text.append("\n")
        self.query_one("#rows", Static).update(text)
        self.query_one("#preview", Static).update(self.preview())

    def preview(self):
        if self.problem():
            return Content.assemble(
                (f"{self.problem()}\n", "$text-error"),
                "Fix the file by hand; until then ZERO runs on the defaults.",
            )
        setting = self.rows[self.selected]
        if setting is None:
            return "Each action's keys: Enter opens them."
        default = controller.setting_text(setting, setting.default,
                                     self.zero.default_model())
        return f"{controller.WHEN[setting.group]}\nDefault: {default}."

    def action_move(self, step):
        self.selected = (self.selected + step) % len(self.rows)
        self.draw()

    def action_change(self):
        if self.problem():
            self.notify("config.toml can't be read: fix it first", severity="error")
            return
        setting = self.rows[self.selected]
        if setting is None:
            self.app.push_screen(KeysView(self.zero))
            return
        now = self.value(setting)
        if setting.kind in ("choice", "mode"):
            choices = (setting.choices if setting.kind == "choice"
                       else self.zero.modes())
            self.app.push_screen(
                ChoiceView(setting.label, list(choices), now),
                lambda value: value and self.set(setting, value),
            )
        elif setting.kind == "model":
            self.pick_model(setting, now)
        else:
            shown = "" if now is None else controller.setting_text(setting, now)
            empty = setting.off or controller.setting_text(setting, setting.default)
            self.app.push_screen(
                PromptView(f"{setting.label} (empty: {empty})", shown),
                lambda text: self.typed(setting, text),
            )

    def pick_model(self, setting, now):
        unset = controller.setting_text(setting, None, self.zero.default_model())
        other = "another model…"
        named = controller.model_choices(
            self.zero.defaults.get("model"), self.zero.lists.sessions,
            self.zero.served_models(),
        )

        def chosen(value):
            if value == other:
                self.app.push_screen(
                    PromptView(f"{setting.label}: its name", now or ""),
                    lambda text: text and text.strip()
                    and self.set(setting, text.strip()),
                )
            elif value is not None:
                self.set(setting, None if value == unset else value)

        self.app.push_screen(
            ChoiceView(setting.label, [unset, *named, other], now or unset), chosen
        )

    def typed(self, setting, text):
        if text is None:
            return
        value, problem = controller.parse_setting(setting, text)
        if problem:
            self.notify(problem, severity="error")
            return
        self.set(setting, value)

    def set(self, setting, value):
        problem = settings.check(setting, value)
        if problem:
            self.notify(problem, severity="error")
            return
        try:
            if setting.name in config.BY_NAME:
                self.zero.save_settings(**{setting.name: value})
            else:
                self.zero.set_setting(setting.name, value)
                self.fetch()
        except (*ERRORS, OSError) as error:
            self.notify(str(error), severity="error")
        self.draw()

    def action_reset(self):
        setting = self.rows[self.selected]
        if setting is not None and not self.problem():
            self.set(setting, setting.default)


class KeysView(Screen):
    """Each action's keys, grouped by where they work. Enter takes the next
    key pressed for the chosen action (CaptureView); Del puts its default
    back. A key one action already has where this one works is moved only
    when asked, and never from an action it would leave with none."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", show=False),
        Binding("up", "move(-1)", show=False, priority=True),
        Binding("down", "move(1)", show=False, priority=True),
        Binding("enter", "change", show=False, priority=True),
        Binding("delete,backspace", "reset", show=False, priority=True),
    ]

    def __init__(self, zero):
        super().__init__()
        self.zero = zero
        self.selected = 0
        self.ids = [
            id for place in keybindings.PLACES
            for id, action in keybindings.ACTIONS.items() if action.where == place
        ]

    def compose(self):
        yield Plain("Key bindings", classes="view-title")
        yield VerticalScroll(Plain(id="rows"))
        yield Plain(id="preview")
        yield Plain("↑↓ choose · Enter, then the new key · Del the default · "
                     "Esc back", classes="view-keys")

    def on_mount(self):
        self.draw()

    def draw(self):
        overrides = self.zero.config["keys"]
        text = Text()
        place = None
        for index, id in enumerate(self.ids):
            action = keybindings.ACTIONS[id]
            if action.where != place:
                place = action.where
                text.append(f"\n{keybindings.PLACES[place]}\n", style="dim")
            style = "reverse" if index == self.selected else ""
            shown = keybindings.shown(self.zero.keys[id])
            changed = id in overrides
            text.append(f"{shown:<26}", style=f"{style} bold" if changed else style)
            text.append(f" {action.what}\n", style=style)
        self.query_one("#rows", Static).update(text)
        action = keybindings.ACTIONS[self.ids[self.selected]]
        default = keybindings.shown(keybindings.split(action.keys))
        self.query_one("#preview", Static).update(f"Default: {default}.")

    def action_move(self, step):
        self.selected = (self.selected + step) % len(self.ids)
        self.draw()

    def action_change(self):
        id = self.ids[self.selected]
        self.app.push_screen(
            CaptureView(keybindings.ACTIONS[id].what),
            lambda key: self.captured(id, key),
        )

    def captured(self, id, key):
        if key is None:
            return
        overrides = dict(self.zero.config["keys"])
        taken = keybindings.clashes(id, key, overrides)
        if not taken:
            self.bind(id, key, overrides)
            return
        said = "; ".join(keybindings.ACTIONS[other].what for other in taken)
        stuck = [other for other in taken if len(self.zero.keys[other]) == 1]
        if stuck:
            self.notify(f"{keybindings.pretty(key)} already does: {said}. Give that "
                        "another key first.", severity="warning")
            return
        move = "Move it here"

        def answered(choice):
            if choice != move:
                return
            for other in taken:
                rest = [k for k in self.zero.keys[other] if k != key]
                overrides[other] = ",".join(rest)
            self.bind(id, key, overrides)

        self.app.push_screen(
            ChoiceView(f"{keybindings.pretty(key)} already does: {said}.",
                       [move, "Leave it there"]),
            answered,
        )

    def bind(self, id, key, overrides):
        overrides[id] = key
        self.save(overrides)

    def action_reset(self):
        id = self.ids[self.selected]
        overrides = dict(self.zero.config["keys"])
        if id not in overrides:
            return
        del overrides[id]
        for key in keybindings.split(keybindings.ACTIONS[id].keys):
            taken = keybindings.clashes(id, key, overrides)
            if taken:
                said = "; ".join(keybindings.ACTIONS[other].what for other in taken)
                self.notify(f"Its default {keybindings.pretty(key)} now does: {said}.",
                            severity="warning")
                return
        self.save(overrides)

    def save(self, overrides):
        # A key set back to its default is no change
        overrides = {
            id: bound for id, bound in overrides.items()
            if bound != keybindings.ACTIONS[id].keys
        }
        try:
            self.zero.save_settings(keys=overrides)
        except OSError as error:
            self.notify(str(error), severity="error")
        self.draw()
