"""The CLI's small views: a page of text, a prompt, a choice, a key
captured for a binding."""

from textual.binding import Binding
from textual.content import Content
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Input, OptionList, Static

from apps.tui import keybindings
from apps.tui.widgets import Plain


class TextView(Screen):
    """Help or jobs: one text, the whole window; Esc goes back."""

    BINDINGS = [Binding("escape", "app.pop_screen", show=False),
                Binding("q", "app.pop_screen", show=False)]

    def __init__(self, title, body):
        super().__init__()
        self.title_text = title
        self.body = body

    def compose(self):
        yield Plain(self.title_text, classes="view-title")
        yield VerticalScroll(Plain(self.body, id="help"))
        yield Plain("Esc back", classes="view-keys")


class PromptView(ModalScreen):
    """One line asked: a title, a workspace, a yes. Enter answers; Esc drops.
    A secret one (an API key) shows dots for what is typed."""

    BINDINGS = [Binding("escape", "dismiss(None)", show=False)]

    def __init__(self, label, text="", secret=False):
        super().__init__()
        self.label = label
        self.text = text
        self.secret = secret

    def compose(self):
        with Vertical():
            yield Plain(self.label)
            yield Input(value=self.text, id="answer", password=self.secret)

    def on_mount(self):
        self.query_one("#answer").focus()

    def on_input_submitted(self, event):
        event.stop()
        self.dismiss(event.value)


class ChoiceView(ModalScreen):
    """A pick list: ↑↓ and Enter choose; Esc drops. The current one is marked."""

    BINDINGS = [Binding("escape", "dismiss(None)", show=False)]

    def __init__(self, label, choices, current=None):
        super().__init__()
        self.label = label
        self.choices = choices
        self.current = current

    def compose(self):
        with Vertical():
            yield Plain(self.label)
            yield OptionList(*(
                Content(f"{'●' if choice == self.current else ' '} {choice}")
                for choice in self.choices
            ))

    def on_mount(self):
        options = self.query_one(OptionList)
        if self.current in self.choices:
            options.highlighted = self.choices.index(self.current)
        options.focus()

    def on_option_list_option_selected(self, event):
        event.stop()
        self.dismiss(self.choices[event.option_index])


class KeyCaptureView(ModalScreen):
    """Takes the next key pressed, whatever it is: the app's own keys are off
    while it shows (Zero.check_action). Esc cancels. A plain character would
    type into the input, so it isn't taken."""

    def __init__(self, what):
        super().__init__()
        self.what = what

    def compose(self):
        yield Plain(self.ask())

    def ask(self, problem=""):
        return f"{problem}Press the new key for: {self.what}\n\nEsc cancels."

    def on_key(self, event):
        event.stop()
        event.prevent_default()
        if event.key == "escape":
            self.dismiss(None)
        elif event.key in keybindings.RESERVED_KEYS:
            self.query_one(Static).update(self.ask(
                f"{keybindings.pretty(event.key)} moves in the lists and edits the "
                "input, so it stays theirs.\n\n"
            ))
        elif event.is_printable and not event.key.startswith(("ctrl+", "alt+")):
            self.query_one(Static).update(self.ask(
                f"{event.key} types into the input: choose a key with Ctrl or "
                "Alt, or a function key.\n\n"
            ))
        else:
            self.dismiss(event.key)
