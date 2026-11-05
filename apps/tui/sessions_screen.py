"""The sessions view: who needs you first, then the sessions by folder."""

import time

from rich.text import Text
from textual.binding import Binding
from textual.content import Content
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Input, Static

from apps.tui import controller
from apps.tui.widgets import ERRORS, Plain
from client import render, session_list


class SessionsView(Screen):
    """The session list, and nothing else (Ctrl+O)."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", show=False),
        Binding("up", "move(-1)", show=False, priority=True),
        Binding("down", "move(1)", show=False, priority=True),
        Binding("ctrl+r", "act('rename')", show=False, priority=True, id="rename"),
        Binding("ctrl+a", "act('archive')", show=False, priority=True,
                id="archive"),
        Binding("delete", "act('delete')", show=False, priority=True, id="trash"),
        Binding("ctrl+n", "elsewhere", show=False, priority=True, id="elsewhere"),
        Binding("tab", "app.pop_screen", show=False, priority=True,
                id="switch_view"),
    ]

    def __init__(self, zero):
        super().__init__()
        self.zero = zero
        self.selected = 0
        self.show_archived = False
        self.archived = []

    def compose(self):
        yield Plain("Sessions", classes="view-title", id="sessions-title")
        yield Input(id="filter", placeholder="filter by title or folder")
        yield VerticalScroll(Plain(id="rows"))
        yield Plain(id="preview")
        yield Plain(id="sessions-keys", classes="view-keys")

    def on_mount(self):
        self.query_one("#filter").focus()
        self.draw()

    def on_input_changed(self, event):
        self.selected = 0
        self.draw()

    def on_input_submitted(self, event):
        event.stop()
        self.open_selected()

    def entries(self):
        rows = self.archived if self.show_archived else self.zero.lists.sessions
        typed = self.query_one("#filter", Input).value
        waiting, rest = session_list.groups(rows, typed)
        entries = [] if self.show_archived else [("new", None, None)]
        entries += [("row", row, "Waiting for you") for row in waiting]
        heading = "Archived" if self.show_archived else "The rest"
        entries += [("row", row, heading) for row in rest]
        return entries

    def draw(self):
        entries = self.entries()
        self.selected = max(0, min(self.selected, len(entries) - 1))
        text = Text()
        group = None
        now = time.time()
        for index, (kind, row, heading) in enumerate(entries):
            if heading is not None and heading != group:
                group = heading
                text.append(f"\n{heading}\n", style="dim")
            style = "reverse" if index == self.selected else ""
            if kind == "new":
                line = (f"+ New session in {session_list.short(self.zero.cwd)}   "
                        f"({self.zero.key('elsewhere')}: in another folder)")
            else:
                line = (f"{session_list.row_mark(row)} {row['title'][:40]:<40} "
                        f"{session_list.brief(row['workspace'], 26):<26} "
                        f"{session_list.row_state(row, now)}")
            text.append(line + "\n", style=style)
        if len(entries) <= (0 if self.show_archived else 1):
            text.append("\n(no sessions)\n", style="dim")
        self.query_one("#rows", Static).update(text)
        title = "Archived sessions" if self.show_archived else "Sessions"
        self.query_one("#sessions-title", Static).update(title)
        archive, other = ("unarchive", "sessions") if self.show_archived else (
            "archive", "archived"
        )
        key = self.zero.key
        self.query_one("#sessions-keys", Static).update(
            f"Enter open · {key('switch_view')} back · {key('elsewhere')} new "
            f"elsewhere · {key('rename')} rename · {key('archive')} {archive} · "
            f"{key('trash')} trash · {key('sessions')} {other}"
        )
        self.query_one("#preview", Static).update(self.preview())

    def preview(self):
        kind, row = self.chosen()
        if row is None:
            return ""
        where = session_list.short(row["workspace"])
        parts = [(f"{row['title']} · {where}\n", "bold")]
        ask = row.get("ask")
        if ask is not None:
            parts += [("? ", "bold $text-warning"), ask["command"]]
            reason = render.reason(ask.get("why"))
            if reason:
                parts.append((f"\n{reason}", "dim"))
        else:
            parts.append(f"{row['state']} · {row['mode']} · {row['model']}")
        return Content.assemble(*parts)

    def chosen(self):
        entries = self.entries()
        if not entries:
            return None, None
        kind, row, _ = entries[max(0, min(self.selected, len(entries) - 1))]
        return kind, row

    def action_move(self, step):
        self.selected += step
        self.draw()

    def action_archived(self):
        self.show_archived = not self.show_archived
        if self.show_archived:
            try:
                self.archived = self.zero.connection.sessions(archived=True)
            except ERRORS as error:
                self.notify(str(error), severity="error")
        self.selected = 0
        self.draw()

    def open_selected(self):
        kind, row = self.chosen()
        self.app.pop_screen()
        try:
            if kind == "new":
                self.zero.new_session(self.zero.cwd)
            elif row is not None:
                self.zero.open(row["id"])
        except ERRORS as error:
            self.zero.note(f"ZERO: {error}")

    def action_act(self, what):
        kind, row = self.chosen()
        zero = self.zero
        if row is None:
            return
        sid = row["id"]
        try:
            if what == "rename":
                zero.ask_line("New title", row["title"],
                              lambda text: zero.connection.rename(sid, text))
            elif what == "archive":
                zero.connection.archive(sid, not row.get("archived", False))
                if self.show_archived:
                    self.archived = zero.connection.sessions(archived=True)
            elif what == "delete":
                zero.confirm_delete(sid, after=self.draw)
        except ERRORS as error:
            self.notify(str(error), severity="error")

    def action_elsewhere(self):
        self.zero.ask_line("Folder for the new session (empty: /)",
                           self.zero.cwd, self.new_in)

    def new_in(self, text):
        self.app.pop_screen()
        self.zero.new_session(controller.resolve_workspace(text, self.zero.cwd))
