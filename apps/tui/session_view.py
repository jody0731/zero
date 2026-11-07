"""The session view: the transcript, and docked at the bottom the attention
line, the input or the ask's box, and the status line."""

from textual.binding import Binding
from textual.content import Content
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Static

from apps.tui import controller
from apps.tui.widgets import MessageBox, Plain, Transcript, placeholder


class SessionView(Screen):
    BINDINGS = [
        Binding("pageup", "page(-1)", show=False, id="page_up"),
        Binding("pagedown", "page(1)", show=False, id="page_down"),
        Binding("ctrl+t", "toggle_folds", show=False, id="toggle_folds"),
        Binding("ctrl+end", "to_end", show=False, priority=True, id="to_end"),
        # While a / list shows: choose in it, complete the chosen one
        Binding("up", "choose(-1)", show=False, priority=True),
        Binding("down", "choose(1)", show=False, priority=True),
        # Completes the chosen command while a / list shows; else the list
        # of sessions
        Binding("tab", "tab", show=False, priority=True, id="switch_view"),
        Binding("shift+tab", "app.cycle_mode", show=False, priority=True,
                id="cycle_mode"),
    ]

    def __init__(self):
        super().__init__()
        self.offered = []
        self.choice = 0

    def check_action(self, action, parameters):
        if action == "choose":
            return bool(self.offered)
        return True

    def on_screen_resume(self):
        # Back from another view: what changed meanwhile shows at once, and
        # the cursor is in the input, with no click after each switch
        self.app.draw()
        self.app.focus_input()

    def offer(self, names):
        """The slash commands the input could be, listed above it."""
        if names != self.offered:
            self.offered, self.choice = names, 0
        self.draw_offer()

    def draw_offer(self):
        listing = self.query_one("#commands", Static)
        listing.display = bool(self.offered)
        parts = []
        for n, name in enumerate(self.offered):
            here = n == self.choice
            parts.append("\n" if n else "")
            parts.append((f"{name:<11}", "bold reverse" if here else "bold"))
            parts.append((f" {controller.COMMANDS[name]}", "" if here else "dim"))
        listing.update(Content.assemble(*parts))

    def chosen(self):
        """The command chosen in the list, if one shows."""
        return self.offered[self.choice] if self.offered else None

    def action_choose(self, step):
        self.choice = (self.choice + step) % len(self.offered)
        self.draw_offer()

    def action_tab(self):
        if not self.offered:
            self.app.action_sessions()
            return
        self.query_one("#message", MessageBox).value = self.chosen() + " "

    def compose(self):
        yield Plain(id="title")
        yield Transcript(id="transcript")
        with Vertical(id="bottom"):
            yield Plain(id="more")
            yield Plain(id="attention")
            with Vertical(id="askbox"):
                yield Plain(classes="head")
                yield MessageBox(id="reply", placeholder="your answer")
            yield Plain(id="progress")
            yield Plain(id="commands")
            yield MessageBox(id="message", placeholder=placeholder(self.app.keys))
            yield Plain(id="status")

    def action_toggle_folds(self):
        self.query_one(Transcript).toggle_folds()

    def action_to_end(self):
        self.query_one(Transcript).to_end()

    def action_page(self, direction):
        transcript = self.query_one(Transcript)
        if direction > 0:
            transcript.scroll_page_down()
        else:
            transcript.scroll_page_up()
