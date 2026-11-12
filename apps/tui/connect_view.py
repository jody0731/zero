"""Connecting a model: the servers ZERO knows, from the server's `servers`.

One view, two doors: "Connect a model" opens in front of the session view
when new sessions' model can't start (its key missing, no window, or none
set with no server connected), and "Models and keys" from the settings.
With a server connected and none set, new sessions' model is picked
instead (`pick_default`). A connect is a few small steps (a key, the
server's models, a name, a window: only those the server doesn't already
answer), then the server's `connect` with all of them in one request:
the server keeps nothing between the steps, so the same request could come
from anywhere. A key is typed into a masked line and goes straight to the
server, which keeps it in keys.toml; the view never shows it.
"""

import re
import threading
from urllib.parse import urlparse

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

import paths
from apps.tui import controller
from apps.tui.dialogs import ChoiceView, PromptView
from apps.tui.widgets import ERRORS, Plain
from client import session_list

# The last row: a server the catalog doesn't know
OWN = "Your own server…"
# The model list's last choice
OTHER = "another model…"
# The pick of new sessions' model: its title and its last choice
PICK = "New sessions' model"
CONNECT = "Connect a model…"
KEY_STATE = {"there": "key saved", "none needed": "no key needed",
             "missing": "needs a key"}
URL_LABEL = ("Its URL (OpenAI-style servers serve under /v1, as "
             "http://localhost:8080/v1)")
NO_MODEL_YET = ("ZERO needs a model to talk to. Pick a server and paste its API "
                "key, or point ZERO at a server of your own (llama.cpp, Ollama, LM "
                "Studio, vLLM).")


def name_from(url):
    """A server's short name, suggested from its URL."""
    server = (urlparse(url).hostname or "").lower()
    if not server or server in ("localhost", "127.0.0.1", "::1"):
        return "local"
    if server.replace(".", "").isdigit():
        return "server"
    return re.sub(r"[^a-z0-9_-]", "-", server.split(".")[0]).strip("-") or "server"


def server_of(model, names):
    """The server a model name goes to, as the server reads it."""
    if not model:
        return None
    prefix, _, rest = model.partition("/")
    return prefix if rest and prefix in names and prefix != "ollama" else "ollama"


def connected_servers(listed):
    """The servers of a `servers` reply a model can be picked from: a key
    saved, or one of the user's own that needs none."""
    return [row for row in listed if row["key_state"] == "there"
            or (row["user_added"] and row["key_state"] != "missing")]


def pick_default(zero):
    """New sessions' model, picked where a server is connected and none is
    set: the models the sessions used, then the servers' (asked off the
    screen's thread, since a server may be slow to list), another typed, and
    "Connect a model…" last. The pick is the default."""

    def listed():
        zero.wait_for_list(1)
        return controller.model_choices(None, zero.lists.sessions,
                                        zero.served_models())

    def offer(named):
        zero.push_screen(ChoiceView(PICK, [*named, OTHER, CONNECT]), chosen)

    def chosen(value):
        if value == CONNECT:
            zero.push_screen(ServersView(zero, first=True))
        elif value == OTHER:
            zero.push_screen(PromptView(f"{PICK}: its name", ""),
                             lambda text: text and text.strip()
                             and take(text.strip()))
        elif value is not None:
            take(value)

    def take(model):
        def taken():
            zero.note(f"ZERO: new sessions use {model}; /settings changes it.")
            zero.focus_input()

        zero.set_setting("model", model, then=taken)

    def run():
        named = listed()
        try:
            zero.call_from_thread(offer, named)
        except RuntimeError:
            # The full screen closed meanwhile
            pass

    threading.Thread(target=run, daemon=True).start()


def parse_context_window(text):
    """A context window typed as 32768, 32,768 or 32_768; None if it isn't one."""
    digits = re.sub(r"[,_ ]", "", text or "")
    return int(digits) if digits.isdigit() and int(digits) > 0 else None


class Connecting:
    """One connect, a step at a time, each a small view; the last the server's
    connect, run off the screen's thread, since a local server may first
    load the model. `row`: a server the server listed; None for one of the
    user's own, not yet known. `new_key`: ask for its key even where none
    is needed."""

    def __init__(self, view, row=None, new_key=False):
        self.view = view
        self.app = view.app
        self.connection = view.zero.connection
        self.row = row
        self.new_key = new_key
        self.name = row["name"] if row else None
        self.title = row["title"] if row else "The server"
        # Only a new server's: a known one's stays where it is
        self.url = None
        # None: the saved one (or none); "": none at all
        self.key = None
        self.model = None
        self.listed = {}

    def start(self):
        if self.row is None:
            self.app.push_screen(PromptView(URL_LABEL), self.url_given)
        elif self.row["needs_key"] or self.new_key:
            self.ask_key()
        else:
            self.list_models()

    def run_in_background(self, work, then):
        """`work()` on a thread of its own; `then(result, error)` back on
        the screen's."""
        def run():
            try:
                result, error = work(), None
            except ERRORS as e:
                result, error = None, e
            self.app.call_from_thread(then, result, error)

        threading.Thread(target=run, daemon=True).start()

    def url_given(self, text):
        if not text or not text.strip():
            return
        self.url = text.strip()
        self.ask_key()

    def ask_key(self):
        if self.row and self.row["key_state"] == "there":
            empty = "keep the saved one"
        elif self.row and self.row["needs_key"]:
            empty = None
        else:
            empty = "no key"
        label = f"{self.title}'s API key"
        if empty:
            label += f" (empty: {empty})"
        self.app.push_screen(PromptView(label, secret=True), self.key_given)

    def key_given(self, text):
        if text is None:
            return
        text = text.strip()
        if text:
            self.key = text
        elif self.row and self.row["key_state"] == "there":
            self.key = None
        elif self.row and self.row["needs_key"]:
            self.view.notify(f"{self.title} needs its API key.",
                             severity="warning")
            return
        else:
            self.key = ""
        self.list_models()

    def list_models(self):
        if self.url:
            fields = {"url": self.url}
        else:
            fields = {"name": self.name}
        if self.key is not None:
            fields["key"] = self.key
        self.view.notify(f"Asking {self.title} for its models…")
        self.run_in_background(lambda: self.connection.probe(**fields),
                               self.models_listed)

    def models_listed(self, result, error):
        found = []
        if error is not None:
            # The test call is the proof; a list that failed only leaves
            # fewer to pick from
            self.view.notify(str(error), severity="warning")
        else:
            url, found = result
            if self.url:
                self.url = url
        self.listed = {m["id"]: m["context_window"] for m in found}
        named = list(self.row["models"]) if self.row else []
        choices = [*self.listed, *(m for m in named if m not in self.listed),
                   OTHER]
        self.app.push_screen(ChoiceView(f"{self.title}: which model?", choices),
                             self.model_chosen)

    def model_chosen(self, choice):
        if choice is None:
            return
        if choice == OTHER:
            self.app.push_screen(
                PromptView("The model's name, as the server knows it"),
                self.model_typed,
            )
            return
        self.model = choice
        self.after_model()

    def model_typed(self, text):
        if text and text.strip():
            self.model = text.strip()
            self.after_model()

    def after_model(self):
        if self.name is None:
            self.app.push_screen(
                PromptView("A short name for the server (its models' names "
                           "start with it)", name_from(self.url)),
                self.name_given,
            )
        else:
            self.ask_context_window()

    def name_given(self, text):
        if not text or not text.strip():
            return
        self.name = text.strip().lower()
        self.ask_context_window()

    def known_context_window(self):
        if self.listed.get(self.model):
            return self.listed[self.model]
        if self.row:
            return (self.row["models"].get(self.model)
                    or self.row["context_window"])
        return None

    def ask_context_window(self):
        if self.known_context_window():
            # The server takes it from the server's list or its table
            self.connect(None)
            return
        self.app.push_screen(
            PromptView(f"{self.model}'s context window, in tokens (the server "
                       "doesn't say; its own setting, as llama-server's "
                       "--ctx-size)"),
            self.context_window_given,
        )

    def context_window_given(self, text):
        if text is None:
            return
        context_window = parse_context_window(text)
        if context_window is None:
            self.view.notify("A context window is a number of tokens, as 32768.",
                             severity="warning")
            self.ask_context_window()
            return
        self.connect(context_window)

    def connect(self, context_window):
        fields = {"url": self.url, "key": self.key, "context_window": context_window}
        self.view.notify(f"Testing {self.model}…")
        self.run_in_background(lambda: self.connection.connect(self.name, self.model,
                                                               **fields),
                               self.view.connected)


class ServersView(Screen):
    """Every server ZERO knows, a row each, then "Your own server…".
    `first`: opened because new sessions' model can't start; Enter on a
    server connects it. From the settings, Enter offers what can be done
    with it: connect a model, a new key, a test, remove."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", show=False),
        Binding("up", "move(-1)", show=False, priority=True),
        Binding("down", "move(1)", show=False, priority=True),
        Binding("enter", "change", show=False, priority=True),
    ]

    def __init__(self, zero, first=False):
        super().__init__()
        self.zero = zero
        self.first = first
        self.selected = 0
        self.rows = [None]
        self.default = None
        self.not_ready_reason = None

    def compose(self):
        if self.first:
            title = "Connect a model"
        else:
            where = session_list.tilde_path(str(paths.models_file()))
            title = f"Models and keys · {where}"
        yield Plain(title, classes="view-title")
        if self.first:
            yield Plain(NO_MODEL_YET, id="first")
        yield VerticalScroll(Plain(id="rows"))
        yield Plain(id="preview")
        yield Plain("↑↓ choose · Enter connect · Esc back", classes="view-keys")

    def on_mount(self):
        self.fetch()
        self.draw()

    def fetch(self):
        """The servers, asked on the requests thread; drawn again once they
        come."""
        self.zero.request(self.zero.connection.servers, then=self.fetched)

    def fetched(self, reply):
        listed = reply["servers"]
        # The catalog's in its order, then the user's own
        ordered = ([r for r in listed if r["in_catalog"]]
                   + [r for r in listed if not r["in_catalog"]])
        self.rows = [*ordered, None]
        self.default = reply["default"]
        self.not_ready_reason = reply["not_ready_reason"]
        # A view closed before the answer came has nothing to draw into
        if self.query("#rows"):
            self.draw()

    def draw(self):
        self.selected = max(0, min(self.selected, len(self.rows) - 1))
        names = [r["name"] for r in self.rows if r]
        using = server_of(self.default, names)
        text = Text()
        for index, row in enumerate(self.rows):
            style = "reverse" if index == self.selected else ""
            if row is None:
                text.append(f"\n{OWN}\n", style=style)
                continue
            mark = "●" if row["name"] == using else " "
            yours = row["user_added"] and not row["in_catalog"]
            title = row["title"] + (" (yours)" if yours else "")
            text.append(f"{mark} {title:<40} ", style=style)
            text.append(KEY_STATE[row["key_state"]], style=style)
            text.append("\n")
        self.query_one("#rows", Static).update(text)
        self.query_one("#preview", Static).update(self.preview())

    def preview(self):
        row = self.rows[self.selected]
        if row is None:
            return ("A server of your own: its URL, a key if it takes one, and "
                    "a model it serves.")
        said = [row["url"]]
        if row["name"] == server_of(self.default, [r["name"] for r in self.rows
                                                    if r]):
            said.append(f"New sessions use {self.default}.")
        if self.not_ready_reason and self.default:
            said.append(self.not_ready_reason)
        return "\n".join(said)

    def action_move(self, step):
        self.selected = (self.selected + step) % len(self.rows)
        self.draw()

    def action_change(self):
        row = self.rows[self.selected]
        if row is None or self.first:
            Connecting(self, row).start()
            return
        actions = ["Connect a model…"]
        if row["needs_key"] or row["user_added"]:
            actions.append("New key…")
        actions.append("Test")
        if row["key_state"] == "there" or row["user_added"]:
            actions.append("Remove")
        self.app.push_screen(ChoiceView(row["title"], actions),
                             lambda action: self.act(row, action))

    def act(self, row, action):
        if action == "Connect a model…":
            Connecting(self, row).start()
        elif action == "New key…":
            Connecting(self, row, new_key=True).start()
        elif action == "Test":
            self.notify(f"Asking {row['title']} for its models…")
            Connecting(self, row).run_in_background(
                lambda: self.zero.connection.probe(name=row["name"]),
                lambda result, error: self.tested(row, result, error),
            )
        elif action == "Remove":
            self.app.push_screen(
                ChoiceView(f"Remove {row['title']}? Its key and your lines "
                           "for it go.", ["Remove", "Keep it"]),
                lambda answer: answer == "Remove" and self.take_back(row),
            )

    def tested(self, row, result, error):
        if error is not None:
            self.notify(str(error), severity="error")
            return
        _, models = result
        self.notify(f"{row['title']} answered: {len(models)} models listed, "
                    "so its URL and key work.")

    def take_back(self, row):
        self.zero.request(lambda: self.zero.connection.disconnect(row["name"]),
                          then=lambda reply: self.taken_back(row, reply))

    def taken_back(self, row, reply):
        said = f"Removed {row['title']}'s key and your lines for it."
        if reply["default_unset"]:
            said += " New sessions have no model: pick one in Default → Model."
            self.zero.defaults["model"] = None
        self.notify(said)
        self.fetch()
        self.draw()

    def connected(self, reply, error):
        """The server's answer to a connect (Connecting.connect)."""
        if error is not None:
            self.notify(str(error), severity="error")
            return
        model = reply["model"]
        said = f"Connected {model}: its test call answered."
        if reply["default"]:
            self.zero.defaults["model"] = model
            said += " New sessions use it."
        self.notify(said)
        self.fetch()
        self.draw()
        if self.first:
            self.app.pop_screen()
            self.zero.connected(model)
        elif not reply["default"]:
            self.app.push_screen(
                ChoiceView(f"Use {model} for new sessions?", ["Yes", "No"]),
                lambda answer: answer == "Yes" and self.make_default(model),
            )

    def make_default(self, model):
        self.zero.set_setting("model", model, then=self.fetch)
