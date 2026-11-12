"""The CLI's app: full screen, the terminal is the whole interface (Textual).

Each view takes the whole window: the session view (the transcript, which
scrolls, and docked at the bottom the attention line, the input or the ask's
box, and the status line), the sessions view, help and jobs. Who said what is
drawn apart (Codex, OpenCode): your messages after a ›, ZERO's as markdown
after a dim •, both plain; each command in a box of its own, one line with its
outcome until clicked. ZERO's own themes (dark and light) follow the desktop's
setting unless /settings picks one, or one of Textual's.

What lines and keys mean is in controller.py, testable with no terminal; the
views are their own files (session_view, session_list_view, settings_view,
dialogs, widgets). The session in view is followed with `session/watch`, the
list with `session/list` and `watch`, each in a thread whose events reach the
screen as messages; what is shown is reported back with `session/mark_seen`, so any
`zero` knows what is news.
"""

import shutil
import subprocess
import threading
import time
import traceback
from datetime import datetime

from textual import events
from textual.app import App
from textual.binding import Binding
from textual.content import Content
from textual.message import Message
from textual.screen import Screen
from textual.theme import Theme
from textual.widgets import Static, TextArea

import paths
import settings
from apps.tui import clipboard, config, controller, keybindings
from apps.tui.connect_view import ServersView, connected_servers, pick_default
from apps.tui.controller import SessionInput
from apps.tui.dialogs import KeyCaptureView, ChoiceView, PromptView, TextView
from apps.tui.requests import Requests
from apps.tui.session_view import SessionView
from apps.tui.session_list_view import SessionListView
from apps.tui.settings_view import SettingsView
from apps.tui.widgets import ERRORS, MessageBox, Plain, Transcript, placeholder
from client import render, session_list
from client.follower import Follower
from client.render import jobs_text


# Records a switch shows at most; a scroll to the top pages back by as many
MAX_RECORDS_SHOWN = 50

# In /fork's list: all of the conversation, not one of the user's messages
ALL_OF_IT = "all"

# ZERO's themes: a blue-black ground; cyan is ZERO (its mark, the
# input, what's new), amber only what needs you (an ask, a waiting command, the
# attention line), violet the mode you set
THEMES = {
    "dark": Theme(
        name="zero-dark", primary="#2DD4D4", secondary="#8B7CF6",
        accent="#8B7CF6", warning="#F5B841", error="#F0566E", success="#4ADE80",
        foreground="#D8E1E8", background="#0B0F14", surface="#111821",
        panel="#16202B", dark=True,
    ),
    "light": Theme(
        name="zero-light", primary="#007C83", secondary="#6A55D8",
        accent="#6A55D8", warning="#B87800", error="#C8304A", success="#1E8A45",
        foreground="#17212B", background="#F3F6F8", surface="#E9EEF2",
        panel="#DDE4EA", dark=False,
    ),
}
# Asks the desktop for its theme; tests set it to None
GDBUS = shutil.which("gdbus")
# The progress line's spinner, a frame per draw
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

CSS = """
Screen { background: $background; }
#title { dock: top; height: 1; padding: 0 1; background: $panel; color: $text-muted; }
#transcript { padding: 0 1; margin: 1 0 1 0; }
.message { margin: 1 0 0 0; height: auto; }
.message .mark { width: 2; }
.message .body { height: auto; width: 1fr; }
.you .mark { color: $text; text-style: bold; }
.you .words { text-style: bold; }
.zero .mark { color: $text-primary; }
.zero Markdown { margin: 0; padding: 0; }
.zero Markdown > *:last-child { margin-bottom: 0; }
.tool { background: $panel; border-left: thick $foreground 25%; padding: 0 1;
        margin: 1 0 0 2; }
.tool.failed { border-left: thick $error; }
.tool.waiting { border-left: thick $text-warning; }
.note { color: $text-muted; text-style: italic; margin: 1 0 0 0; }
.note.failed { color: $text-error; text-style: not italic; }
#bottom { dock: bottom; height: auto; }
#more { height: 1; color: $text-primary; padding: 0 1; display: none; }
#attention { height: 1; color: $text-warning; padding: 0 1; display: none; }
#progress { height: 1; padding: 0 1; color: $text-muted; display: none; }
#commands { height: auto; padding: 0 2; display: none; color: $text-muted; }
#askbox { height: auto; border: round $text-warning; padding: 0 1; margin: 0 1;
          display: none; }
MessageBox { height: auto; max-height: 10; margin: 0 1; }
/* Round, as the other boxes: Textual's tall border draws its left edge as a
   reversed block, and a terminal that draws the block a pixel short shows a
   second, faint line beside it */
MessageBox, Input { border: round $border-blurred; }
MessageBox:focus, Input:focus { border: round $border; }
#status { height: 1; background: $panel; color: $text-muted; padding: 0 1;
          margin: 1 0 0 0; }
.view-title { dock: top; height: 1; padding: 0 1; background: $panel;
               text-wrap: nowrap; text-overflow: ellipsis; }
.view-keys { dock: bottom; height: 1; padding: 0 1; background: $panel;
             color: $text-muted; }
#rows { padding: 0 1; }
#preview { height: auto; max-height: 8; padding: 0 1; color: $text-muted;
           border-top: solid $foreground 25%; }
#help { padding: 1 2; }
PromptView, ChoiceView { align: center middle; background: $background 60%; }
PromptView Vertical, ChoiceView Vertical { width: 70%; height: auto;
    border: round $primary; padding: 1 2; background: $panel; }
ChoiceView OptionList { height: auto; max-height: 12; margin: 1 0 0 0; }
KeyCaptureView { align: center middle; background: $background 60%; }
KeyCaptureView Static { width: 60%; height: auto; border: round $text-warning;
    padding: 1 2; background: $panel; }
"""


class ListWatch:
    """Keeps the session list current from its watch, across server restarts;
    after a `bye` "stopped" it waits until `resume()`."""

    def __init__(self, connection, changed):
        self.connection = connection
        self.changed = changed
        self.sessions = []
        self.leaving = threading.Event()
        self.stopped = False
        self.stream = None
        self.thread = None

    def start(self):
        self.stopped = False
        self.thread = threading.Thread(target=self.run, name="watch-list",
                                       daemon=True)
        self.thread.start()

    def run(self):
        while not self.leaving.is_set():
            stream = None
            try:
                stream = self.stream = self.connection.watch_list()
                for event in stream:
                    if "sessions" in event:
                        self.sessions = event["sessions"]
                        self.changed()
            except ERRORS + (ValueError,):
                pass
            finally:
                self.stream = None
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            if stream is not None and stream.bye == "stopped":
                self.stopped = True
                return
            self.leaving.wait(0.5)

    def resume(self):
        if self.stopped and not self.leaving.is_set():
            self.start()

    def stop(self):
        self.leaving.set()
        stream = self.stream
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


class WorkFromThread(Message):
    """Work a thread hands the screen without waiting for it (call_from_thread
    would wait, and stall a switch that joins that thread)."""

    def __init__(self, work, *args):
        super().__init__()
        self.work = work
        self.args = args


class SilentRenderer:
    """The follower's renderer: the screen draws records itself."""

    def event(self, event):
        return []


class SafeClicks:
    """Textual 8.2's #6643: a click that lands while a
    Markdown widget updates (a reply streaming in, ten times a second) can be
    handed a paragraph the update has just detached, and Textual's text
    selection crashes on its missing parent, taking the CLI down. Such a
    click is dropped: what it pointed at is gone."""

    async def on_event(self, event):
        if isinstance(event, events.MouseDown) and self.lands_detached(event):
            return
        await super().on_event(event)

    def lands_detached(self, event):
        try:
            widget, _offset = self.screen.get_widget_and_offset_at(event.x, event.y)
        except Exception:
            return False
        return (widget is not None and not isinstance(widget, Screen)
                and widget.parent is None)


class ZeroApp(SafeClicks, App):
    CSS = CSS
    TITLE = "ZERO"
    BINDINGS = [
        Binding("ctrl+c", "interrupt", show=False, priority=True, id="interrupt"),
        Binding("ctrl+d", "leave", show=False, priority=True, id="leave"),
        Binding("ctrl+o", "sessions", show=False, priority=True, id="sessions"),
        Binding("alt+down", "next_needing_you", show=False, priority=True,
                id="next_needing_you"),
        Binding("alt+up", "previous_session", show=False, priority=True,
                id="previous_session"),
        Binding(keybindings.ACTIONS["follow_up"].keys, "follow_up", show=False,
                priority=True, id="follow_up"),
    ]

    def __init__(self, connection, cwd, mode=None, model=None, defaults=None,
                 older=lambda: False, session=None, show_list=False, notice=None):
        super().__init__()
        self.connection = connection
        self.cwd = cwd
        self.mode = mode
        self.model = model
        self.defaults = defaults or {}
        self.older = older
        self.first = session
        self.show_list = show_list
        # Why this view opened again (reopening): said once it's up
        self.notice = notice
        self.client = SessionInput(connection, None, mode, model, workspace=cwd)
        self.follower = None
        self.thread = None
        self.back = []
        self.oldest = None
        self.last_key = 0.0
        # The ask whose box is open: once open, it stays until the ask closes
        self.boxed = None
        # The box's choice Enter sends when nothing is typed
        self.picked = 0
        # Which follower is current: a stopped one's late events are dropped
        self.generation = 0
        self.lists = ListWatch(connection, lambda: self.run_on_screen(self.listed))
        # Every request to the server goes through it, off the drawing thread
        self.requests = Requests(self.run_on_screen)
        # The session an `open` was last asked for: an earlier one's answer
        # that comes in after is dropped
        self.opening = None
        self.view = SessionView()
        # config.toml's settings (/settings); a broken file is said, not used
        self.config_error = None
        # What this ZERO doesn't know of the file, said once it opens
        self.config_notes = []
        try:
            self.config = config.load(notes=self.config_notes)
        except settings.SettingsError as error:
            self.config = {**config.DEFAULTS, "keys": {}}
            self.config_error = str(error)
        self.keys = keybindings.current_keys(self.config["keys"])

    # Starting and leaving

    async def on_mount(self):
        self.apply_settings()
        # Mounted before anything is drawn into it
        await self.push_screen(self.view)
        self.view.query_one("#reply", MessageBox).pick = self.pick_key
        if self.config_error:
            self.note(f"ZERO: {self.config_error}; running on the defaults.")
        for note in self.config_notes:
            self.note(f"ZERO: {note}.")
        if self.notice:
            self.note(self.notice)
        self.lists.start()
        if self.first is not None:
            self.open(self.first, remember=False)
        else:
            self.new_session(self.client.workspace)
        if self.show_list:
            self.action_sessions()
        if self.model is None:
            self.offer_connect()
        self.set_interval(0.25, self.draw)
        self.focus_input()

    def offer_connect(self):
        """When new sessions' model can't start: with none set and a server
        connected, a pick of the models known (pick_default); otherwise
        "Connect a model" in front (nothing connected, its key missing, no
        window). A server that can't say leaves it to the first message's
        refusal."""
        self.request(self.connection.servers, then=self.connect_offered,
                     failed=ignored)

    def connect_offered(self, reply):
        if not reply.get("not_ready_reason"):
            return
        servers = connected_servers(reply.get("servers", ()))
        if not reply.get("default") and servers:
            pick_default(self)
        else:
            self.push_screen(ServersView(self, first=True))

    def connected(self, model):
        """A model connected from "Connect a model": the session view is
        back, and new sessions start on it."""
        self.note(f"ZERO: connected {model}. Write your first message.")
        self.draw()
        self.focus_input()

    def on_unmount(self):
        self.lists.stop()
        self.stop_following()
        self.requests.stop()

    def request(self, work, then=None, failed=None):
        """`work()`, a call to the server, on the requests thread, never the
        drawing one (design rule 12); `then(result)` back on the screen, and
        an error `failed`'s, said in the transcript by default."""
        self.requests.submit(work, then, failed or self.failed)

    def wait_for_list(self, seconds=2):
        """For a thread of its own: the session list, once it came."""
        deadline = time.monotonic() + seconds
        while not self.lists.sessions and time.monotonic() < deadline:
            time.sleep(0.02)

    def action_leave(self):
        self.exit(self.client.eof())

    # The session in view

    def row(self, sid=None):
        sid = sid or self.client.session
        for row in self.lists.sessions:
            if row["id"] == sid:
                return row
        return None

    @property
    def transcript(self):
        return self.view.query_one(Transcript)

    def open(self, sid, remember=True):
        """Show a session: its last MAX_RECORDS_SHOWN records, a line for the earlier
        ones, which a scroll to the top brings in, then it goes live."""
        if remember and self.client.session and self.client.session != sid:
            self.back.append(self.client.session)
        self.stop_following()
        self.opening = sid
        known = self.row(sid)

        def fetch():
            row = known or find(self.connection.sessions(), sid)
            records, gap = self.connection.history(sid, after=0,
                                                   limit=MAX_RECORDS_SHOWN)
            return row, records, gap

        self.request(fetch, then=lambda found: self.show(sid, *found))

    def show(self, sid, row, records, gap):
        """An opened session's records, fetched (open)."""
        if sid != self.opening:
            return
        self.stop_following()
        self.client = SessionInput(self.connection, sid, workspace=row["workspace"])
        transcript = self.transcript
        transcript.clear()
        if gap:
            transcript.line(earlier_line(gap), classes=EARLIER_CLASSES)
        for record in records:
            self.client.event({"record": record})
            transcript.add(record, self.connection.source, follow=False)
        self.oldest = records[0]["seq"] if records else None
        self.loading = False
        if self.client.ask is None and row.get("ask") is None and (
            row["state"] in ("idle", "failed")
        ):
            self.client.running = False
        after = records[-1]["seq"] if records else 0
        self.follow(after)
        self.mark_seen(after)
        self.boxed = None
        self.draw()
        self.focus_input()

    def new_session(self, workspace):
        if self.client.session:
            self.back.append(self.client.session)
        self.stop_following()
        self.opening = None
        self.client = SessionInput(
            self.connection, None, self.mode, self.model, workspace=workspace
        )
        self.oldest = None
        self.transcript.clear()
        self.draw()

    def listed(self):
        """A new session list from the server: every view that shows it is
        drawn again, the list itself too when it is the one showing."""
        self.draw()
        if isinstance(self.screen, SessionListView) and self.screen.is_mounted:
            self.screen.draw()

    def run_on_screen(self, work, *args):
        """Run `work(*args)` on the screen's loop; safe from any thread."""
        self.post_message(WorkFromThread(work, *args))

    def on_work_from_thread(self, message):
        message.work(*message.args)

    def follow(self, after):
        self.generation += 1
        generation = self.generation
        self.follower = Follower(
            self.connection, self.client, SilentRenderer(), after=after,
            emit=lambda line: self.run_on_screen(self.followed_line, generation, line),
            on_event=lambda event: self.run_on_screen(self.followed, event, generation),
        )
        self.thread = threading.Thread(target=self.follower.run,
                                       name="watch-client", daemon=True)
        self.thread.start()

    def stop_following(self):
        """The follower is told to stop and ends on its own thread, never
        waited for here: what it hands on late carries an old generation
        and is dropped."""
        self.generation += 1
        if self.follower is not None:
            self.follower.stop()
        self.follower = self.thread = None

    def followed_line(self, generation, line):
        if generation == self.generation:
            self.note(line)

    def followed(self, event, generation=None):
        if generation is not None and generation != self.generation:
            return
        record = event.get("record")
        if record is not None and record["type"] == "rewind":
            # The conversation went back: its view is the branch it left
            self.open(self.client.session, remember=False)
            return
        if record is not None:
            self.transcript.add(record, self.connection.source)
            if record["type"] in ("turn_end", "ask"):
                self.mark_seen(record["seq"])
        if "draft" in event:
            self.transcript.draft(event["draft"])
        elif "settled" in event:
            self.transcript.undraft()
        for message in event.get("withdrawn", []):
            self.transcript.unsend(message["id"])
        lines = self.client.take_withdrawn()
        if lines:
            field = self.view.query_one("#message", MessageBox)
            field.value = " ".join(lines + ([field.value] if field.value else []))
        self.draw()

    def mark_seen(self, seq):
        sid = self.client.session
        if sid is None:
            return
        self.request(lambda: self.connection.mark_seen(sid, seq), failed=ignored)

    def note(self, text):
        self.transcript.line(text)

    # Lines and commands

    def on_text_area_changed(self, event):
        if event.text_area.id == "message":
            self.last_key = time.monotonic()
            self.view.offer(controller.completions(event.text_area.text))

    def on_message_box_submitted(self, event):
        if event.input.id == "message":
            chosen = self.view.chosen()
            if chosen is not None:
                event.input.value = chosen
            self.submit(event.input)
        elif event.input.id == "reply":
            text = event.value
            event.input.value = ""
            if text.strip():
                self.send(text, reply=True)
            else:
                self.choose(self.picked)

    def pick_key(self, key):
        """↑/↓ move among the box's choices, a number picks one; True when the
        key was the box's."""
        if not self.box_showing():
            return False
        offered = controller.choices(self.client.ask)
        if key in ("up", "down"):
            step = -1 if key == "up" else 1
            self.picked = (self.picked + step) % len(offered)
            self.draw()
            return True
        if key.isdigit() and 1 <= int(key) <= len(offered):
            self.choose(int(key) - 1)
            return True
        return False

    def choose(self, index):
        """Send one of the box's choices: its label as the line, its id as
        the choice, so no consent check reads it."""
        offered = controller.choices(self.client.ask or {})
        if not 0 <= index < len(offered):
            return
        choice, label = offered[index]
        self.send(label, reply=True, choice=choice)

    def action_follow_up(self):
        focused = self.focused
        if isinstance(focused, MessageBox) and focused.id == "message":
            self.submit(focused, follow_up=True)

    def submit(self, field, follow_up=False):
        text = field.value
        field.value = ""
        found = controller.command(text)
        if found is not None:
            self.command(*found)
            return
        if self.client.leaves(text):
            self.action_leave()
            return
        self.send(text, follow_up)

    def send(self, text, follow_up=False, reply=False, choice=None):
        if not text.strip():
            return
        client = self.client

        def work():
            # Asked here, in turn: a line before it may have made the session
            made = client.session is None
            return made, client.line(text, follow_up=follow_up, choice=choice)

        self.request(work, then=lambda sent: self.sent(client, text, *sent))

    def sent(self, client, text, made, answer):
        """A line the server took: shown as said, and followed."""
        if answer is None or client is not self.client:
            return
        handled_as = answer.get("handled_as")
        mark = {"steered": "steer", "queued": "queued"}.get(handled_as)
        self.transcript.sent(text, answer.get("id"), mark)
        if made or self.follower is None or self.follower.stopped:
            # A new session, or the server was stopped by hand and starts again
            after = self.follower.after if self.follower and not made else 0
            self.follow(after)
        self.lists.resume()
        self.draw()

    def failed(self, error):
        """A request that went wrong, said in the transcript and popped up
        (a dim line among a turn's output goes unseen): the client stays
        up whatever a command or a line runs into."""
        if isinstance(error, ERRORS):
            said = f"ZERO: {error}"
        else:
            said = f"ZERO: {type(error).__name__}: {error}"
        self.note(said)
        self.notify(said, severity="warning")

    def command(self, name, rest):
        sid = self.client.session
        try:
            if name in ("/quit", "/bye"):
                self.action_leave()
            elif name == "/help":
                body = controller.help_text(self.config["keys"])
                self.push_screen(TextView("Help", body))
            elif name == "/settings":
                self.push_screen(SettingsView(self))
            elif name == "/init":
                self.send(controller.INIT)
            elif name == "/new":
                self.ask_line("Workspace for the new session (empty: /)", self.cwd,
                              lambda text: self.new_session(
                                  controller.resolve_workspace(text, self.cwd)))
            elif name == "/sessions":
                self.action_sessions()
            elif name == "/mode":
                if rest:
                    self.switch(mode=rest)
                else:
                    self.push_screen(
                        ChoiceView("Mode, from the next turn", self.modes(),
                                   self.session_value("mode")),
                        lambda mode: mode and self.switch(mode=mode),
                    )
            elif name == "/model":
                if rest:
                    self.switch(model=rest)
                else:
                    # The model and its thinking, as in /settings
                    self.push_screen(SettingsView(self, place="model"))
            elif name == "/cd":
                here = self.session_value("workspace") or self.cwd
                if rest:
                    self.switch(workspace=controller.resolve_workspace(rest, here))
                else:
                    def chosen(text):
                        folder = controller.resolve_workspace(text, here)
                        self.switch(workspace=folder)

                    self.ask_line("Folder its commands run in", here, chosen)
            elif sid is None:
                self.note("This session isn't made yet: send a message first.")
            elif name == "/continue":
                self.request(lambda: self.connection.continue_session(sid),
                             then=self.continued)
            elif name == "/rename":
                if rest:
                    self.request(lambda: self.connection.rename(sid, rest))
                else:
                    title = (self.row() or {}).get("title", "")
                    self.ask_line("New title", title, lambda text: self.request(
                        lambda: self.connection.rename(sid, text)))
            elif name == "/compact":
                self.request(lambda: self.connection.compact(sid),
                             then=lambda reply: self.note(
                                 render.COMPACTING[reply["compacting"]]))
            elif name == "/rewind":
                self.pick_message(
                    sid, "Rewind to before which of your messages? Its text goes "
                    "back into the input; files stay as they are.", self.rewind_to,
                )
            elif name == "/fork":
                self.pick_message(
                    sid, "A new session from this conversation, up to before "
                    "which of your messages?", self.fork_from, whole=True,
                )
            elif name == "/archive":
                archived = not (self.row() or {}).get("archived", False)
                self.request(lambda: self.connection.archive(sid, archived),
                             then=lambda _: self.note(
                                 "Archived: hidden from the list." if archived
                                 else "Back in the list."))
            elif name == "/delete":
                self.confirm_delete(sid)
            elif name == "/copy":
                if self.client.last_reply:
                    self.copy(self.client.last_reply)
                else:
                    self.note("No answer to copy yet.")
            elif name == "/permissions":
                if sid is None:
                    self.note("ZERO: no session yet.")
                    return
                self.request(lambda: self.connection.grants(sid),
                             then=lambda grants: self.granted(sid, grants, rest))
            elif name == "/jobs":
                self.request(lambda: self.connection.jobs(sid), then=self.listed_jobs)
        except Exception as error:
            self.failed(error)

    def continued(self, reply):
        self.transcript.sent("/continue", reply.get("id"))
        self.client.running = True
        self.client.doing("thinking")

    def granted(self, sid, grants, rest):
        """/permissions: the session's grants shown, or one taken back."""
        words = rest.split()
        if len(words) == 2 and words[0] == "forget" and words[1].isdigit():
            index = int(words[1]) - 1
            if not 0 <= index < len(grants):
                self.note(f"ZERO: no permission {words[1]}.")
                return
            grant = grants[index]
            self.request(lambda: self.connection.forget_grant(sid, grant["id"]),
                         then=lambda _: self.note(
                             f"ZERO: taken back: {grant['description']}."))
            return
        self.push_screen(TextView("Permissions", render.grants_text(grants)))

    def listed_jobs(self, jobs):
        body = jobs_text(jobs) if jobs else "No jobs in this session."
        self.push_screen(TextView("Jobs", body))

    def pick_message(self, sid, label, then, whole=False):
        """A list of the user's messages on the conversation's current branch,
        newest first, to rewind or fork from; `whole`: all of it on top."""
        self.request(lambda: self.connection.user_messages(sid),
                     then=lambda messages: self.offer_messages(
                         sid, label, then, whole, messages))

    def offer_messages(self, sid, label, then, whole, messages):
        if not messages and not whole:
            self.note("No message of yours to go back to yet.")
            return
        choices, values = [], []
        if whole:
            choices.append("All of the conversation")
            values.append(ALL_OF_IT)
        for found in messages:
            when = datetime.fromisoformat(found["time"]).strftime("%d %b %H:%M")
            lines = found["text"].strip().splitlines() or [""]
            choices.append(f"{when}  {lines[0][:70]}")
            values.append(found["seq"])
        self.push_screen(ChoiceView(label, choices, values=values),
                         lambda value: value is not None and then(sid, value))

    def rewind_to(self, sid, seq):
        """Rewind, then show the branch it leaves, the message in the input."""

        def rewound(reply):
            self.open(sid, remember=False)
            self.fill_input(reply["text"])

        self.request(lambda: self.connection.rewind(sid, seq), then=rewound)

    def fork_from(self, sid, value):
        """Fork, then open the new session, the message in its input."""

        def forked(reply):
            self.open(reply["session"]["id"])
            self.fill_input(reply["text"])

        seq = None if value == ALL_OF_IT else value
        self.request(lambda: self.connection.fork(sid, seq), then=forked)

    def fill_input(self, text):
        field = self.view.query_one("#message", MessageBox)
        field.value = text
        self.focus_input()

    def session_value(self, name):
        """The session's mode, model or workspace as it stands (the next
        turn's, after a switch)."""
        row = self.row() or {}
        own = getattr(self.client, name)
        if self.client.session is None:
            return own or self.defaults.get(name)
        return row.get(name) or own

    def switch(self, **values):
        """Set what the session runs with: the server keeps it for the next
        turn; a session not made yet is made with it."""
        sid = self.client.session
        try:
            if sid is None:
                if "mode" in values:
                    values["mode"] = controller.mode_named(values["mode"], self.modes())
                for name, value in values.items():
                    setattr(self.client, name, value)
                said = controller.switched_text(values)
                self.notify(f"This session will start with {said[:1].lower()}"
                            f"{said[1:]}.")
            else:
                self.request(lambda: self.connection.set(sid, **values),
                             then=self.switched)
        except Exception as error:
            self.failed(error)
        self.draw()

    def switched(self, reply):
        said = controller.switched_text(reply["set"])
        when = "from the next turn" if reply["next_turn"] else "from now"
        self.notify(f"{said}, {when}.")
        self.lists.resume()
        self.draw()

    def action_cycle_mode(self):
        if self.screen is self.view:
            self.switch(mode=controller.next_mode(self.session_value("mode")))

    def on_transcript_at_top(self, message):
        self.load_earlier()

    def load_earlier(self):
        """Earlier records, put in above what shows when a scroll reaches the
        top, which stays on what showed while they come in above it."""
        sid = self.client.session
        if self.oldest is None or self.oldest <= 1:
            return
        if getattr(self, "loading", False):
            return
        self.loading = True
        oldest = self.oldest

        def failed(error):
            self.loading = False
            self.failed(error)

        self.request(lambda: self.connection.history(
            sid, after=0, limit=MAX_RECORDS_SHOWN, before=oldest
        ), then=lambda found: self.earlier(sid, *found), failed=failed)

    def earlier(self, sid, records, gap):
        """Earlier records, fetched (load_earlier), put in above."""
        if sid != self.client.session:
            self.loading = False
            return
        transcript = self.transcript
        made = []
        for record in records:
            made += transcript.blocks(record, self.connection.source)
        head = earlier_line(gap) if gap else "── the start ──"
        # The old line of earlier records goes; the new one leads
        for widget in list(transcript.children)[:1]:
            if widget.has_class("earlier"):
                widget.remove()
        made.insert(0, Plain(head, classes=EARLIER_CLASSES if gap else "note"))
        below = transcript.max_scroll_y - transcript.scroll_y
        transcript.mount(*made, before=0)
        if records:
            self.oldest = records[0]["seq"]

        def settle():
            # The same distance from the end as before: the same view
            transcript.following = False
            transcript.held = max(0, transcript.max_scroll_y - below)
            transcript.scroll_to(y=transcript.held, animate=False,
                                 immediate=True, release_anchor=False)
            self.loading = False

        transcript.call_after_refresh(settle)

    def confirm_delete(self, sid, after=None):
        row = self.row(sid) or {"title": sid}

        def delete(text):
            if (text or "").strip().lower() not in ("y", "yes"):
                return
            self.request(lambda: self.connection.delete(sid), then=deleted)

        def deleted(where):
            self.notify(f"Moved “{row['title']}” to {where}.")
            if sid == self.client.session:
                self.new_session(self.cwd)
            if after is not None:
                after()

        self.ask_line(f"Move “{row['title']}” to the trash? y or n", "", delete)

    def ask_line(self, label, text, done):
        def answered(value):
            if value is None:
                return
            try:
                done(value)
            except ERRORS as error:
                self.notify(str(error), severity="error")

        self.push_screen(PromptView(label, text), answered)

    def notify(self, message, *, markup=True, **options):
        # Never markup, whoever asks: a notice may name a title or a command
        super().notify(message, markup=False, **options)

    # Settings

    def modes(self):
        """The modes the server offers (its `hello`): the client knows none."""
        return list(self.defaults.get("modes") or controller.MODES_KNOWN)

    def served_models(self):
        """The models every server lists, asked of the server; none when it
        can't say, since they are only suggestions."""
        try:
            return controller.served_models(self.connection.models())
        except Exception:
            return []

    def default_model(self):
        """The server's own model, where config.toml names none."""
        return self.defaults.get("default_model")

    def runs_with(self):
        """What the session in view runs with from its next turn: the server's
        word for a made one; for one not made yet, what it will be made with
        where the user chose it (the rest is the defaults')."""
        if self.client.session is not None:
            return self.connection.runs_with(self.client.session)
        chosen = {
            "mode": self.client.mode or self.defaults.get("mode"),
            "model": (self.client.model or self.defaults.get("model")
                      or self.default_model()),
            "thinking_effort": self.client.thinking_effort,
        }
        return {name: value for name, value in chosen.items() if value}

    def first_key(self, id):
        """The first of an action's keys, as the views' hints name it."""
        return keybindings.pretty(self.keys[id][0])

    def apply_settings(self):
        """The theme and the keys as config.toml has them, at once."""
        for theme in THEMES.values():
            self.register_theme(theme)
        self.theme = theme_for(self.config["theme"])
        self.keys = keybindings.current_keys(self.config["keys"])
        self.set_keymap(keybindings.keymap(self.config["keys"]))
        if self.view.is_mounted:
            field = self.view.query_one("#message", MessageBox)
            field.placeholder = placeholder(self.keys)

    def save_settings(self, **changes):
        """Write a change to the full screen's own settings ([tui]) and show it
        at once."""
        values = {**self.config, **changes}
        config.save(values)
        self.config = values
        self.apply_settings()
        self.draw()

    def set_setting(self, name, value, then=None):
        """Change one of ZERO's settings through the server, which reads it for
        its next session; `then()` once it has."""

        def done(_):
            # What a session not made yet starts with
            if name == "mode":
                self.defaults["mode"] = value
            if name == "model":
                self.defaults["model"] = value or self.default_model()
            # The status line shows a new session's mode and model
            self.draw()
            if then is not None:
                then()

        self.request(lambda: self.connection.set_setting(name, value), then=done)

    def check_action(self, action, parameters):
        # While a new key is captured, every key goes to the capture
        return not isinstance(self.screen, KeyCaptureView)

    # Moving between sessions

    def action_sessions(self):
        if isinstance(self.screen, SessionListView):
            self.screen.action_archived()
            return
        self.push_screen(SessionListView(self))

    def action_next_needing_you(self):
        rows = session_list.needs_you(self.lists.sessions, self.client.session)
        if not rows:
            self.notify("Nothing else needs you.")
            return
        self.leave_views()
        try:
            self.open(rows[0]["id"])
        except ERRORS as error:
            self.note(f"ZERO: {error}")

    def action_previous_session(self):
        self.leave_views()
        while self.back:
            sid = self.back.pop()
            if sid != self.client.session:
                try:
                    self.open(sid, remember=False)
                except ERRORS as error:
                    self.note(f"ZERO: {error}")
                return
        self.notify("No session before this one.")

    def leave_views(self):
        while self.screen is not self.view:
            self.pop_screen()

    def action_interrupt(self):
        """Copy a selection, the input's or the screen's; else stop the moving
        turn, clear the input, or, twice on an empty input, leave."""
        focused = self.focused
        if isinstance(focused, TextArea) and focused.selected_text:
            self.copy(focused.selected_text)
            return
        selected = self.screen.get_selected_text()
        if selected:
            self.copy(selected)
            self.screen.clear_selection()
            return
        if self.screen is not self.view:
            self.pop_screen()
            return
        field = self.view.query_one("#message", MessageBox)
        empty = not field.value
        field.value = ""

        def stop(sid):
            self.request(lambda: self.connection.stop(sid))

        if self.client.ctrl_c(empty, stop=stop):
            self.action_leave()
        if self.client.notice:
            self.notify(self.client.notice)
            self.client.notice = None

    def copy(self, text):
        """Put `text` on the desktop's clipboard and say where it went."""
        took = clipboard.copy(text, osc52=self.copy_to_clipboard)
        size = f"{len(text):,} character{'s' if len(text) != 1 else ''}"
        self.notify(f"Copied {size} (through {took}).", timeout=2)

    # Drawing

    def box_showing(self):
        ask = self.client.ask
        if ask is None:
            return False
        if self.boxed != ask["ask"] and self.client.box_open(self.last_key):
            self.boxed = ask["ask"]
            self.picked = 0
        return self.boxed == ask["ask"]

    def focus_input(self):
        target = "#reply" if self.box_showing() else "#message"
        if self.screen is self.view:
            self.view.query_one(target, MessageBox).focus()

    def draw(self):
        view = self.view
        # Textual's shutdown stops the app running before it takes the views'
        # parts down, and the draw timer can fire in between
        if not view.is_mounted or not self.is_running:
            return
        row = self.row()
        if self.client.session is None:
            where = session_list.tilde_path(self.client.workspace or "/")
            title = f"new session · {where}"
        else:
            title = (f"{(row or {}).get('title', self.client.session)} · "
                     f"{session_list.tilde_path((row or {}).get('workspace', '/'))}")
        view.query_one("#title", Static).update(title)
        transcript = self.transcript
        if transcript.max_scroll_y - transcript.scroll_y <= 1:
            transcript.unread = 0
        more = view.query_one("#more", Static)
        more.display = transcript.unread > 0
        more.update(f"↓ {transcript.unread} new below · PgDn")
        line = session_list.attention_line(self.lists.sessions, self.client.session)
        attention = view.query_one("#attention", Static)
        attention.display = bool(line)
        attention.update(line)
        box = view.query_one("#askbox")
        showing = self.box_showing()
        was = box.display
        box.display = showing
        view.query_one("#message", MessageBox).display = not showing
        if showing:
            view.offer([])
        if showing:
            box.query_one(".head", Static).update(self.ask_text())
        if showing != was:
            self.focus_input()
        doing = controller.progress(self.client, row)
        progress = view.query_one("#progress", Static)
        progress.display = bool(doing) and not showing
        if doing:
            frame = SPINNER[int(time.monotonic() * 4) % len(SPINNER)]
            width = max(10, self.size.width - 4)
            progress.update(f"{frame} {controller.fit(doing, width)}")
        pieces = controller.status_pieces(self.client, row, self.defaults,
                                     older=self.older())
        line = controller.fit(pieces, max(10, self.size.width - 2))
        mode, _, rest = line.partition(" · ")
        parts = [(mode, "bold $text-accent")]
        if rest:
            parts.append(" · " + rest)
        view.query_one("#status", Static).update(Content.assemble(*parts))

    def ask_text(self):
        ask = self.client.ask or {}
        # The command, the agent's reason, what the ask is about, then the one
        # question every ask ends with
        parts = [(f"$ {ask.get('command', '')}", "bold")]
        # An older ask has only its `why`
        reason = (ask.get("reason") if "about" in ask
                  else render.reason(ask.get("why", "")))
        if reason:
            parts.append((f"\n{reason}", "dim"))
        if ask.get("about"):
            parts.append((f"\n{ask['about']}", ""))
        parts += [("\n? ", "bold $text-warning"), (controller.PROCEED, "bold")]
        # The choices: the picked one marked; words typed below go to
        # the consent check instead
        for n, (_, label) in enumerate(controller.choices(ask), 1):
            mark = "›" if n - 1 == self.picked else " "
            style = "bold $text-accent" if n - 1 == self.picked else ""
            parts.append((f"\n{mark} {n}. {label}", style))
        parts.append(("\n  ↑/↓ and Enter, a number, or type an answer", "dim"))
        return Content.assemble(*parts)


def ignored(error):
    """A request whose failure isn't worth a word (a seen mark, say)."""


# The line that says how many earlier records wait above, marked so a later
# load finds it
EARLIER_CLASSES = "note earlier"


def earlier_line(count):
    return f"… {count} earlier records: scroll up for them"


def theme_for(name):
    """The theme for the theme setting: dark and light are ZERO's, auto asks
    the desktop which, any other is one of Textual's by its name."""
    if name in THEMES:
        return THEMES[name].name
    if name == "auto":
        return desktop_theme()
    return name


def desktop_theme(run=subprocess.run):
    """ZERO's dark or light theme, as the desktop's appearance setting has it
    (the freedesktop portal: 1 dark, 2 light); dark when nothing says."""
    if GDBUS is None:
        return THEMES["dark"].name
    try:
        done = run(
            [GDBUS, "call", "--session",
             "--dest", "org.freedesktop.portal.Desktop",
             "--object-path", "/org/freedesktop/portal/desktop",
             "--method", "org.freedesktop.portal.Settings.Read",
             "org.freedesktop.appearance", "color-scheme"],
            capture_output=True, text=True, timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return THEMES["dark"].name
    if done.returncode == 0 and "uint32 2" in done.stdout:
        return THEMES["light"].name
    return THEMES["dark"].name


class KeyName:
    """A key name where Textual's sequence table wants a Keys member."""

    def __init__(self, value):
        self.value = value


def konsole_shift_enter():
    """Konsole sends ESC O M for Shift+Enter (the keypad's Enter), which
    Textual reads as Enter; read it as Shift+Enter, a new line. Terminals
    with the kitty protocol report Shift+Enter as itself."""
    from textual._ansi_sequences import ANSI_SEQUENCES_KEYS

    ANSI_SEQUENCES_KEYS["\x1bOM"] = (KeyName("shift+enter"),)


def find(sessions, sid):
    for row in sessions:
        if row["id"] == sid:
            return row
    raise LookupError(f"no session {sid}")


def run_full_screen(connection, cwd, session=None, mode=None, model=None,
                    defaults=None, show_list=False, older=lambda: False):
    konsole_shift_enter()

    def make(sid, notice):
        first = sid if sid is not None else session
        return ZeroApp(connection, cwd, mode=mode, model=model, defaults=defaults,
                       older=older, session=first,
                       show_list=show_list and notice is None, notice=notice)

    return reopening(make, paths.Paths().crash_log)


# A view that crashes opens again: the server holds the session, so a
# crash costs a moment, never the work. So many within a minute stop it
MAX_CRASHES = 3
CRASH_SECONDS = 60


def reopening(make, log, now=time.monotonic):
    """Run the view `make(session, notice)` builds until it ends without a
    crash; after one, write its traceback to `log` and open the same session
    again, saying why. Returns the exit code."""
    crashes = []
    sid = notice = None
    while True:
        app = make(sid, notice)
        message = app.run(mouse=app.config.get("mouse") == "on")
        # Textual keeps the exception that ended the app (8.2)
        error = getattr(app, "_exception", None)
        if error is None:
            if message:
                print(message)
            return 0
        what = f"{type(error).__name__}: {error}"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"--- {datetime.now().isoformat(timespec='seconds')}\n")
            f.write("".join(traceback.format_exception(error)))
        crashes = [t for t in crashes if now() - t < CRASH_SECONDS] + [now()]
        if len(crashes) >= MAX_CRASHES:
            print(f"ZERO's view crashed {MAX_CRASHES} times within a minute, the last "
                  f"with {what}; the tracebacks are in {log}. The sessions go on "
                  "in the server, and the next zero opens them.")
            return 1
        sid = app.client.session or sid
        notice = (f"ZERO: the view crashed ({what}) and opened again; the "
                  f"session went on in the server. The traceback is in {log}.")
