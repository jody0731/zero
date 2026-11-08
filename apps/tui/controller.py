"""The CLI's meaning of lines and keys; no terminal needed to drive it.

`SessionInput` is the session in view: what a line, Ctrl+C or Ctrl+D does
there, over the session's state (client/session_state.py). The functions below
it decide what the CLI shows around it: the slash commands and help, an ask's
box, the status and progress lines, the mode Shift+Tab steps to, /model's
choices, and the settings view's text. The session list's pieces are
client/session_list.py's, the plain texts client/render.py's.
"""

import os
import time
from pathlib import Path

from apps.tui import keybindings
from client import render
from client.session_list import short_path
from client.session_state import SessionState

# Typed alone, these leave the session as Ctrl+D does
LEAVE = ("exit", "quit", "/bye")
WAITING = "Nothing is running: a question is waiting. Answer it, or type a message."
# How long after the last keystroke an ask's box may take the input's place
BOX_DELAY_SECONDS = 1.0
# What Shift+Tab steps through (Claude Code's key); bypassPermissions only by
# name, typed, so no key lands on it by accident
MODE_CYCLE = ("default", "auto", "auto+", "readOnly")
COMMANDS = {
    "/new": "a new session (asks for its workspace)",
    "/continue": "go on with a turn that ended without an answer",
    "/sessions": "the session list (also Ctrl+O)",
    "/rename": "rename this session: /rename TITLE",
    "/compact": "compact this session's context: now, or before the next step "
                "while a turn runs",
    "/archive": "archive this session (or bring it back)",
    "/delete": "move this session to the trash (asked first)",
    "/jobs": "this session's background jobs",
    "/copy": "copy ZERO's last answer, as its Markdown",
    "/permissions": "what runs without asking (a plan's calls aside); "
                    "/permissions forget N takes one back",
    "/mode": "the mode, from the next turn: /mode NAME, or pick (Shift+Tab cycles)",
    "/model": "the model and its thinking, from the next turn: /model NAME, "
              "or pick",
    "/cd": "the folder its commands run in, from the next turn: /cd FOLDER",
    "/settings": "this session, new sessions' defaults, the TUI, models and "
                 "keys, key bindings, Advanced",
    "/init": "ZERO looks over you and this machine and fills in its ZERO.md",
    "/help": "keys and commands",
    "/quit": "leave; turns and jobs go on in ZERO",
}
# What /init sends: the user's own request, so each change it leads to is
# asked about as any other
INIT = (
    "Get to know me and this machine: look around (who I am, from what's here; "
    "the hardware, the system, the main tools and services I use) and add what "
    "you find to the “The user” and “Machines” sections of my "
    "ZERO.md with the memory tool, one short entry per fact."
)
HELP = """Sending: a new turn, or a steer while one runs; a \\ just before the
send key starts a new line too. A follow-up waits for the answer. /mode, /model
and /cd change the session from its next turn; /settings sets this session, what
new sessions start with, the theme and these keybindings, and in Advanced the
judge and the limits. Scrolling to the top brings earlier records. Click a command to
unfold it; drag over text and Ctrl+C copies it (Shift+drag: the terminal's own
selection); Ctrl+V pastes, Ctrl+A selects the input. The mouse wheel scrolls.
Leaving, turns and jobs go on. Esc leaves a view."""


def help_text(overrides=None):
    """The help view: what sending means, the keys as the user has them, and
    the commands."""
    bound = keybindings.current_keys(overrides)
    width = max(len(keybindings.shown(k)) for k in bound.values())
    lines = [HELP, "", "Keys:"]
    for id, now in bound.items():
        what = keybindings.ACTIONS[id].what
        lines.append(f"  {keybindings.shown(now):<{width}}  {what}")
    lines += ["", "Commands:"]
    lines += [f"  {command:<11} {what}" for command, what in COMMANDS.items()]
    return "\n".join(lines)


class SessionInput(SessionState):
    """The session in view: what a line, Ctrl+C or Ctrl+D does there."""

    def __init__(self, connection, session=None, mode=None, model=None,
                 workspace=None):
        super().__init__(connection, session, mode, model, workspace)
        # A line for the terminal to print once, then clear
        self.notice = None
        self.interrupt_armed = False
        self.leaving = False

    def leaves(self, text):
        """Whether this line means leave, as Ctrl+D does."""
        return text.strip().lower() in LEAVE

    def line(self, text, follow_up=False, choice=None):
        """A line typed disarms Ctrl+C's second press (SessionState.line)."""
        with self.lock:
            self.interrupt_armed = False
            return super().line(text, follow_up, choice)

    def ctrl_c(self, empty=True):
        """Stop a moving turn; a parked one isn't moving, so only say so."""
        with self.lock:
            if self.running:
                self.interrupt_armed = False
                self.connection.stop(self.session)
            elif self.ask is not None:
                self.interrupt_armed = False
                self.notice = WAITING
            elif empty and self.interrupt_armed:
                self.leaving = True
            else:
                self.interrupt_armed = True
            return self.leaving

    def eof(self):
        with self.lock:
            self.leaving = True
            if self.running:
                return "The turn goes on in ZERO; `zero` brings you back to it."
            if self.ask is not None:
                return "The question waits in ZERO; `zero` brings you back to it."
            return ""

    def box_open(self, last_key, now=None):
        """Whether the ask's box shows: an ask is open and the user hasn't
        typed for a second, so typing can't fall into it (Codex)."""
        now = time.monotonic() if now is None else now
        return self.ask is not None and now - last_key >= BOX_DELAY_SECONDS


# The one question every ask ends with (Claude Code's); what it is about and
# why stand above it
PROCEED = "Do you want to proceed?"


# The answers an ask's box offers, picked with ↑/↓ and Enter or their
# number; words typed instead go to the consent check
CHOICE_LABELS = {
    "yes": "Yes",
    "session": "Yes, and don't ask again this session for {what}",
    "saved": "Yes, and don't ask again for {what}, in any session",
    "no": "No",
}


def choices(ask):
    """An ask's choices as (id, label): from its record, else Yes and
    No."""
    ids = ask.get("choices") or ["yes", "no"]
    what = (ask.get("notes") or {}).get("offer_text") or "this"
    return [(c, CHOICE_LABELS[c].format(what=what))
            for c in ids if c in CHOICE_LABELS]


def command(text):
    """(name, rest) when a line is a slash command; None for a message. A
    path such as "/usr/share is big?" is a message: only the names count."""
    word, _, rest = text.strip().partition(" ")
    if word in COMMANDS or word == "/bye":
        return word, rest.strip()
    return None


def completions(text):
    """The slash commands a typed start could be."""
    if not text.startswith("/") or " " in text:
        return []
    return [name for name in COMMANDS if name.startswith(text)]


def status_pieces(client, row, defaults, home=None, older=False, now=None):
    """The status line's pieces, most important first: mode, model,
    workspace, state, the context's meter, and a note when the server runs
    older code."""
    now = time.monotonic() if now is None else now
    mode = (row or {}).get("mode") or client.mode or defaults.get("mode") or "?"
    model = (row or {}).get("model") or client.model or defaults.get("model") or "?"
    if "/" in model:
        model = model.rsplit("/", 1)[-1]
    workspace = short_path((row or {}).get("workspace") or client.workspace or "/", 30,
                           home)
    # What a running or waiting turn still holds from before: its next turn
    pending = (row or {}).get("pending") or ()
    mode, model, workspace = (
        f"{value} (next turn)" if name in pending else value
        for name, value in (("mode", mode), ("model", model),
                            ("workspace", workspace))
    )
    pieces = [mode, model, workspace, state_text(client, row)]
    meter = render.meter_text(getattr(client, "context", None))
    if meter:
        pieces.append(meter)
    if older:
        pieces.append("the server runs older code")
    return pieces


def state_text(client, row):
    """The session's state in a word or two; the progress line says more."""
    if client.session is None:
        return "new session"
    if client.ask is not None:
        return "waiting for you"
    if client.running:
        return "working"
    return (row or {}).get("state", "idle")


def progress(client, row, now=None):
    """What ZERO is doing, for the line above the input: thinking, writing (a
    reply streams in) or a command, with how long, a retry of the model
    call, then its running jobs. Empty when it does nothing, or waits for an
    answer (its box says so)."""
    now = time.monotonic() if now is None else now
    pieces = []
    if client.session is not None and client.ask is None and client.running:
        seconds = int(now - (client.since or now))
        if isinstance(client.activity, tuple):
            command = " ".join(str(client.activity[1]).split())
            pieces.append(f"running {seconds} s: $ {command}")
        elif client.activity == "writing":
            pieces.append(f"writing {seconds} s")
        else:
            pieces.append(f"thinking {seconds} s")
        if client.retry is not None:
            error = " ".join(str(client.retry.get("error", "")).split())
            if len(error) > 60:
                error = error[:59] + "…"
            pieces.append(f"retry {client.retry.get('retry')}: {error}")
    jobs = (row or {}).get("jobs", 0)
    if jobs:
        pieces.append(f"{jobs} job{'' if jobs == 1 else 's'} running")
    return pieces


def next_mode(mode):
    """The mode Shift+Tab goes to from `mode`."""
    if mode not in MODE_CYCLE:
        return MODE_CYCLE[0]
    return MODE_CYCLE[(MODE_CYCLE.index(mode) + 1) % len(MODE_CYCLE)]


def mode_named(name, modes):
    """The server's spelling of a mode typed in any case; as typed if unknown,
    for the server to refuse."""
    for mode in modes:
        if mode.lower() == name.lower():
            return mode
    return name


def model_choices(default, sessions, served=()):
    """/model's pick list: the default, the models the sessions use, then
    the servers', each once."""
    names = [default] + [row.get("model") for row in sessions] + list(served)
    return list(dict.fromkeys(name for name in names if name))


def served_models(reply):
    """The models every server lists, from the server's `models` reply; none
    from a server that couldn't list them."""
    if not reply.get("ok"):
        return []
    return [name for row in reply.get("servers", ()) for name in row["models"]]


def fit(pieces, width, separator=" · "):
    """Drop pieces from the right until the line fits (Hermes)."""
    pieces = list(pieces)
    while len(pieces) > 1 and len(separator.join(pieces)) > width:
        pieces.pop()
    line = separator.join(pieces)
    return line if len(line) <= width else line[: max(0, width - 1)] + "…"


def resolve_workspace(text, cwd):
    """A typed workspace: empty is /, ~ is home, a relative path from cwd."""
    text = text.strip()
    if not text:
        return "/"
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        path = Path(cwd) / path
    return str(path.resolve())


# What the settings views say under each section: when a change counts
WHEN_IT_APPLIES = {
    "Current session": "Applies from this session's next turn, as /mode and "
                       "/model do.",
    "Default": (
        "For sessions made from now: a session keeps what it was made with; "
        "/mode and /model change the session in view."
    ),
    "TUI": "Applies at once; auto follows the desktop's dark or light setting.",
    "Judge": (
        "The judge answers the asks in auto and auto+: its reason, a second "
        "look at it, then its decision. This session's rows apply from its "
        "next turn; the default's to sessions made from now."
    ),
    "Limits": (
        "For sessions made from now: a session keeps the limits it was made "
        "with. Evals keep their own."
    ),
    "Server": "Counts at the server's next look, within a second.",
}
# A line more for a setting whose effect depends on the server
SETTING_NOTES = {
    "thinking_effort": "Sent to DeepSeek; other servers get none.",
    "judge_thinking_effort": "Sent to DeepSeek; other servers get none.",
}
# How a switch names what it changed, in a notice
SWITCHED = {"thinking_effort": "thinking effort", "judge_model": "the judge's model",
            "judge_thinking_effort": "the judge's thinking"}


def switched_text(changes):
    """What a switch set, in words: "Mode auto, the judge's model the
    session's own"."""
    said = []
    for name, value in changes.items():
        if name == "judge_model" and not value:
            value = "the session's own"
        said.append(f"{SWITCHED.get(name, name)} {value}")
    text = ", ".join(said)
    return text[:1].upper() + text[1:]


def setting_text(setting, value, default_model=None):
    """A setting's value as the settings view shows it."""
    if value is None:
        if setting.name in ("model", "judge_model") and default_model:
            return f"{setting.unset_means} ({default_model})"
        # An effort the session was made without: none was sent
        return setting.unset_means or "the API's default"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def parse_setting(setting, text):
    """(value, None) from what was typed for a number, or (None, why not).
    Empty: the setting's default (unset, where it can be)."""
    import settings

    text = text.strip()
    if not text:
        return setting.default, None
    try:
        value = float(text) if setting.kind == "number" else int(text)
    except ValueError:
        kind = "a number" if setting.kind == "number" else "a whole number"
        return None, f"{setting.label}: {kind}, please"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    problem = settings.check(setting, value)
    if problem:
        return None, problem
    return value, None
