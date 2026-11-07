"""The settings views, through the host's `setting/list` and `setting/set`
messages (ZERO's) and config.py (the CLI's own [cli] table), the session in
view through `session/set`, and the key bindings view."""

from dataclasses import dataclass

from rich.text import Text
from textual.binding import Binding
from textual.content import Content
from textual.containers import VerticalScroll
from textual.geometry import Region
from textual.screen import Screen
from textual.widgets import Static

import paths
import settings
from apps.tui import config, controller, keybindings
from apps.tui.connect_screen import ServersView
from apps.tui.dialogs import CaptureView, ChoiceView, PromptView
from apps.tui.widgets import ERRORS, Plain
from client import session_list


# The rows that open another view: the model servers and their keys, the key
# bindings, and Dev
MODELS, KEYS, DEV = "models", "keys", "dev"
LINKS = {MODELS: "Models and keys…", KEYS: "Key bindings…", DEV: "Dev…"}
# What each view lists: the session in view's rows, then the settings of
# these groups, then the links. The judge is a design choice, not a user's
# knob, so it sits in Dev with the limits and the host
PLACES = {
    "main": (("mode", "model", "effort"), ("Default", "TUI"),
             (MODELS, KEYS, DEV)),
    "dev": (("judge_model", "judge_effort"), ("Judge", "Limits", "Host"), ()),
    "model": (("model", "effort"), (), ()),
}
TITLES = {"main": "Settings", "dev": "Settings · Dev",
          "model": "This session's model"}
HEADINGS = {"Judge": "The judge"}


@dataclass(frozen=True)
class Current:
    """A row for the session in view: what its next turn runs with, set
    through `session/set`; `setting` says what it takes."""

    setting: settings.Setting

    @property
    def name(self):
        return self.setting.name


class Scrolled:
    """A view whose rows are one Static in a VerticalScroll: `lines` holds
    each row's line, and the selected one is kept on screen."""

    def reveal(self):
        scroll = self.query_one(VerticalScroll)
        if self.selected == 0:
            # The first row's heading too
            scroll.scroll_home(animate=False, immediate=True)
            return
        line = self.lines[self.selected]
        scroll.scroll_to_region(Region(0, line, 1, 1), animate=False,
                                immediate=True)


class SettingsView(Scrolled, Screen):
    """One of the settings views (`place`): "main", /settings (the session
    in view, new sessions' defaults, the TUI's own, and rows that open the
    model servers, the key bindings and Dev); "dev" (the judge, for the
    session in view and by default, the limits and the host); "model",
    /model (the session's model and thinking). A setting's change is written
    at once; a session's goes to its next turn."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", show=False),
        Binding("up", "move(-1)", show=False, priority=True),
        Binding("down", "move(1)", show=False, priority=True),
        Binding("enter", "change", show=False, priority=True),
        Binding("delete,backspace", "reset", show=False, priority=True),
    ]

    def __init__(self, zero, place="main"):
        super().__init__()
        self.zero = zero
        self.place = place
        self.selected = 0
        # ZERO's settings as the host has them: name → value, each one's
        # Setting, and what is wrong with config.toml, if anything
        self.values = {}
        self.known = {s.name: s for s in settings.SETTINGS}
        self.error = None
        # What the session in view runs with from its next turn
        self.now = {}
        self.rows = []
        # Each row's line in the list, for the scrolling
        self.lines = []

    def fetch(self):
        """ZERO's settings and their values, from the host, and what the
        session in view runs with."""
        try:
            listed, self.error = self.zero.connection.settings()
        except ERRORS as error:
            self.notify(str(error), severity="error")
            listed = []
        try:
            self.now = self.zero.runs_with()
        except ERRORS as error:
            # The session's rows show the defaults' values meanwhile
            self.notify(str(error), severity="error")
            self.now = {}
        shown = []
        for row in listed:
            fields = {k: v for k, v in row.items() if k != "value"}
            setting = settings.Setting(**{**fields, "choices": tuple(row["choices"])})
            self.values[setting.name] = row["value"]
            self.known[setting.name] = setting
            if setting.shown:
                shown.append(setting)
        current, groups, links = PLACES[self.place]
        # A session not made yet takes its judge from the defaults
        made = self.zero.client.session is not None
        rows = [Current(self.known[name]) for name in current
                if name in self.known and (made or self.place != "dev")]
        rows += sorted(
            (s for s in [*config.SETTINGS, *shown] if s.group in groups),
            key=lambda s: groups.index(s.group),
        )
        self.rows = [*rows, *links]

    def value(self, row):
        if isinstance(row, Current):
            if row.name in self.now:
                return self.now[row.name]
            return self.values.get(row.name)
        if row.name in config.BY_NAME:
            return self.zero.config[row.name]
        return self.values[row.name]

    def problem(self):
        """What is wrong with config.toml, as the CLI or the host read it."""
        return self.zero.config_error or self.error

    def compose(self):
        title = TITLES[self.place]
        if self.place != "model":
            title += f" · {session_list.short(str(paths.config_file()))}"
        yield Plain(title, classes="view-title")
        yield VerticalScroll(Plain(id="rows"))
        yield Plain(id="preview")
        yield Plain("↑↓ choose · Enter change · Del back to the default · "
                     "Esc back", classes="view-keys")

    def on_mount(self):
        self.fetch()
        self.draw()

    def on_screen_resume(self):
        # Back from another view, where the default may have changed
        self.fetch()
        self.draw()

    def heading(self, row):
        if isinstance(row, Current):
            return "The judge" if self.place == "dev" else "Current session"
        if row in LINKS:
            return ""
        return HEADINGS.get(row.group, row.group)

    def label(self, row):
        if row in LINKS:
            return LINKS[row]
        # Dev has the judge twice: the session's, then the default
        if isinstance(row, Current):
            if self.place == "dev":
                return f"{row.setting.label} (this session)"
            return row.setting.label
        if self.place == "dev" and row.group == "Judge":
            return f"{row.label} (default)"
        return row.label

    def shown_value(self, row):
        """(the row's value in words, whether it differs from the
        default)."""
        if row == MODELS:
            return "", False
        if row == KEYS:
            mine = self.zero.config["keys"]
            return (f"{len(mine)} changed" if mine else "defaults"), bool(mine)
        if row == DEV:
            return "the judge, the limits, the host", False
        if isinstance(row, Current):
            setting, now = row.setting, self.value(row)
            # The judge's model, unset, is the session's own
            model = self.now.get("model") if row.name == "judge_model" else None
            return controller.setting_text(setting, now, model), False
        now = self.value(row)
        default_model = self.zero.default_model()
        return (controller.setting_text(row, now, default_model),
                now != row.default)

    def draw(self):
        self.selected = max(0, min(self.selected, len(self.rows) - 1))
        text = Text()
        group = None
        self.lines = []
        line = 0
        for index, row in enumerate(self.rows):
            heading = self.heading(row)
            if heading != group:
                group = heading
                text.append(f"\n{heading}\n" if heading else "\n", style="dim")
                line += 2 if heading else 1
            style = "reverse" if index == self.selected else ""
            value, changed = self.shown_value(row)
            text.append(f"{self.label(row):<50} ", style=style)
            text.append(value, style=f"{style} bold" if changed else style)
            text.append("\n")
            self.lines.append(line)
            line += 1
        self.query_one("#rows", Static).update(text)
        self.query_one("#preview", Static).update(self.preview())
        self.call_after_refresh(self.reveal)

    def preview(self):
        if self.problem():
            return Content.assemble(
                (f"{self.problem()}\n", "$text-error"),
                "Fix the file by hand; until then ZERO runs on the defaults.",
            )
        if not self.rows:
            return ""
        row = self.rows[self.selected]
        if row == MODELS:
            return ("The model servers and their API keys: connect a model, "
                    "a new key, a test, remove. Enter opens them.")
        if row == KEYS:
            return "Each action's keys: Enter opens them."
        if row == DEV:
            return ("The judge (for this session and by default), the limits "
                    "and the host. Enter opens them.")
        note = controller.NOTES.get(row.name)
        if isinstance(row, Current):
            when = controller.WHEN["Current session"]
            if row.name.startswith("judge_"):
                when = controller.WHEN["Judge"]
            return f"{when}\n{note}" if note else when
        default = controller.setting_text(row, row.default,
                                          self.zero.default_model())
        lines = [controller.WHEN[row.group], f"Default: {default}."]
        if note:
            lines.insert(1, note)
        return "\n".join(lines)

    def action_move(self, step):
        if self.rows:
            self.selected = (self.selected + step) % len(self.rows)
            self.draw()

    def action_change(self):
        if not self.rows:
            return
        row = self.rows[self.selected]
        if row == MODELS:
            self.app.push_screen(ServersView(self.zero))
            return
        if row == DEV:
            self.app.push_screen(SettingsView(self.zero, place="dev"))
            return
        if isinstance(row, Current):
            self.change_session(row)
            return
        if self.problem():
            self.notify("config.toml can't be read: fix it first", severity="error")
            return
        if row == KEYS:
            self.app.push_screen(KeysView(self.zero))
            return
        now = self.value(row)
        if row.kind in ("choice", "mode"):
            choices = (row.choices if row.kind == "choice"
                       else self.zero.modes())
            self.app.push_screen(
                ChoiceView(row.label, list(choices), now),
                lambda value: value and self.set(row, value),
            )
        elif row.kind == "model":
            # New sessions' model has no unset choice: none set, they
            # can't start; the judge's unset is the session's own model
            self.pick_model(row, now, lambda value: self.set(row, value),
                            unset=row.name != "model")
        else:
            shown = "" if now is None else controller.setting_text(row, now)
            empty = row.off or controller.setting_text(row, row.default)
            self.app.push_screen(
                PromptView(f"{row.label} (empty: {empty})", shown),
                lambda text: self.typed(row, text),
            )

    def change_session(self, row):
        """A row of the session in view: what its next turn runs with."""
        setting, now = row.setting, self.value(row)
        label = f"{setting.label}, from this session's next turn"
        if setting.kind == "model":
            if row.name == "model":
                self.pick_model(setting, now, lambda value: self.switch(row, value),
                                unset=False)
            else:
                # Unset: the session's own model, whatever it is switched to
                self.pick_model(
                    setting, now,
                    lambda value: self.switch(row, value or ""),
                    default_model=self.now.get("model"),
                )
            return
        choices = (self.zero.modes() if setting.kind == "mode"
                   else list(setting.choices))
        self.app.push_screen(
            ChoiceView(label, choices, now),
            lambda value: value and self.switch(row, value),
        )

    def switch(self, row, value):
        self.zero.switch(**{row.name: value})
        self.fetch()
        self.draw()

    def pick_model(self, setting, now, done, unset=True, default_model=None):
        """A model from those known, or another typed; `unset`: the
        setting's own unset choice first, which `done` gets as None."""
        default_model = default_model or self.zero.default_model()
        none = controller.setting_text(setting, None, default_model)
        other = "another model…"
        named = controller.model_choices(
            self.zero.defaults.get("model"), self.zero.lists.sessions,
            self.zero.served_models(),
        )

        def chosen(value):
            if value == other:
                self.app.push_screen(
                    PromptView(f"{setting.label}: its name", now or ""),
                    lambda text: text and text.strip() and done(text.strip()),
                )
            elif value is not None:
                done(None if unset and value == none else value)

        choices = [*([none] if unset else []), *named, other]
        current = now or (none if unset else None)
        self.app.push_screen(ChoiceView(setting.label, choices, current), chosen)

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
        if not self.rows:
            return
        row = self.rows[self.selected]
        if not isinstance(row, settings.Setting) or self.problem():
            return
        if row.name == "model":
            self.notify("New sessions need a model: Enter picks another.")
            return
        self.set(row, row.default)


class KeysView(Scrolled, Screen):
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
        # Each row's line in the list, for the scrolling
        self.lines = []

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
        self.lines = []
        line = 0
        for index, id in enumerate(self.ids):
            action = keybindings.ACTIONS[id]
            if action.where != place:
                place = action.where
                text.append(f"\n{keybindings.PLACES[place]}\n", style="dim")
                line += 2
            style = "reverse" if index == self.selected else ""
            shown = keybindings.shown(self.zero.keys[id])
            changed = id in overrides
            text.append(f"{shown:<26}", style=f"{style} bold" if changed else style)
            text.append(f" {action.what}\n", style=style)
            self.lines.append(line)
            line += 1
        self.query_one("#rows", Static).update(text)
        self.call_after_refresh(self.reveal)
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
