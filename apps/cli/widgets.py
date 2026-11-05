"""The CLI's pieces: the transcript (who said what, each command a fold),
the message box, and the plain blocks the views are made of."""

from textual.binding import Binding
from textual.content import Content
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Markdown, Static, TextArea

from apps.cli import clipboard, keybindings
from client import render
from client.connection import NotRunning, ProtocolError
from client.render import duration, outcome_of, who


ERRORS = (OSError, NotRunning, ProtocolError, LookupError)


class Plain(Static):
    """Text shown as it is: what the log holds (commands, notices, titles) may
    have brackets Textual would read as markup (a curl -w '%{...}' would crash
    it). Styled text is built as Content, never as markup."""

    def __init__(self, content="", **options):
        super().__init__(content, markup=False, **options)


class Fold(Static):
    """A command's block: one line, its command's first line cut to fit and
    its outcome, until clicked (Claude Code) or opened with the others (Ctrl+T,
    with the mouse off); then the whole command and its
    output."""

    def __init__(self, command, **options):
        super().__init__(**options)
        self.command = command
        self.mark = ("… running", "dim")
        self.lines = []
        self.open = False

    def on_mount(self):
        self.draw()

    def on_resize(self):
        self.draw()

    def finish(self, result):
        ok, code, seconds, lines = outcome_of(result)
        took = f" {seconds}" if seconds else ""
        if ok:
            mark = f"✓{took}"
        elif code is None:
            mark = "✗ not run" if result.startswith("Not run") else f"✗{took}"
        else:
            mark = f"✗ exit {code}{took}"
        self.mark = (mark, "$text-success" if ok else "$text-error")
        self.set_class(not ok, "failed")
        self.remove_class("waiting")
        self.lines = lines
        self.draw()

    def waiting(self):
        self.mark = ("⏸ waits for you", "$text-warning")
        self.add_class("waiting")
        self.draw()

    def title(self, room):
        """The command's first line, cut to `room` cells."""
        first = self.command.strip().splitlines()[0] if self.command.strip() else ""
        more = first != self.command.strip()
        if len(first) > room or more:
            first = first[: max(1, room - 2)].rstrip() + " …"
        return first

    def draw(self):
        count = len(self.lines)
        tail = f"  {self.mark[0]}"
        if count and not self.open:
            tail += f"  {count} line{'' if count == 1 else 's'}"
        width = self.content_size.width or 80
        title = self.title(width - 2 - len(tail))
        foldable = bool(self.lines) or title != self.command.strip()
        parts = [("▾ " if self.open else "▸ ", "dim") if foldable else "  ",
                 (title, "bold"), "  ", self.mark]
        if count and not self.open:
            parts.append((f"  {count} line{'' if count == 1 else 's'}", "dim"))
        if self.open:
            if title != self.command.strip():
                parts.append(("\n" + self.command.strip(), "bold"))
            parts += [("\n" + line, "dim") for line in self.lines]
        self.update(Content.assemble(*parts))

    def on_click(self):
        self.open = not self.open
        self.draw()


def message(mark, body, kind):
    """A speaker's message: a mark in the margin, then the words; no box."""
    return Horizontal(
        Plain(mark, classes="mark"), Vertical(body, classes="body"),
        classes=f"message {kind}",
    )


class Said(Static):
    """The user's words, and after them, dim, where they came from when not
    the user's own terminals (a phone, say) or how they went (a steer)."""

    def __init__(self, text, label=None):
        parts = [(text, "bold")]
        if label:
            parts.append((f"  · {label}", "not bold $text-muted"))
        super().__init__(Content.assemble(*parts), classes="words")
        self.said = text
        self.label = label


def yours(text, label=None):
    return message("❯", Said(text, label), "you")


class Transcript(VerticalScroll):
    """The session's records as blocks; it follows the end unless the user
    scrolled up, and then counts what came below. Each message of the user's
    shows once: sent from here it shows as it is sent, and its records (the
    turn's start, a steer, a reply and its answer) carry its inbox id. A
    scroll of the user's that reaches the top says so (AtTop), and earlier
    records come in above."""

    class AtTop(Message):
        """The user scrolled to the top: earlier records are wanted."""

    def __init__(self, **options):
        super().__init__(**options)
        self.calls = {}
        self.started = None
        self.restarts = 0
        self.unread = 0
        self.shown = set()
        self.echoes = {}
        # The reply streaming in: its message, its Markdown and the text
        # it shows, until the next record takes its place
        self.drafting = None
        # Where the user is: following the end, or held where they
        # scrolled to. Textual's anchor let go of a held place whenever the
        # content shrank for a moment (a Markdown update mounts its
        # paragraphs again; a draft gives way to its record): the view was
        # clamped to the end, which the anchor took for a return there
        self.following = True
        self.held = 0.0
        # A scroll of the user's still to be read: a block arriving in
        # between took the view back to the old place over it
        self.scrolling = False
        # Whether the commands' blocks show their output (Ctrl+T)
        self.folds_open = False

    def toggle_folds(self):
        self.folds_open = not self.folds_open
        for fold in self.query(Fold):
            fold.open = self.folds_open
            fold.draw()
        self.call_after_refresh(self.stick)

    def stick(self):
        """After a change: at the end while following it, else back where the
        user scrolled to."""
        if self.following:
            self.scroll_end(animate=False, immediate=True)
        elif not self.scrolling and self.scroll_y != self.held:
            self.scroll_to(y=self.held, animate=False, immediate=True,
                           release_anchor=False)

    def release_anchor(self):
        # Textual calls it for a scroll the user makes with keys or the scroll
        # bar, never for stick's
        super().release_anchor()
        self.user_scrolls()

    # The wheel's scroll doesn't release the anchor (Textual 8.2's
    # _scroll_down_for_pointer); with the mouse on each block took the
    # view back to the held place. These run before Widget's own handlers
    def on_mouse_scroll_down(self, event):
        self.user_scrolls()

    def on_mouse_scroll_up(self, event):
        self.user_scrolls()

    def user_scrolls(self):
        self.scrolling = True
        self.call_after_refresh(self.moved)

    def moved(self):
        target = self.scroll_target_y
        self.following = self.max_scroll_y - target <= 1
        self.held = target
        self.scrolling = False
        if target <= 0:
            self.post_message(self.AtTop())

    def to_end(self):
        """Go to the end and follow it (Ctrl+End, as in Claude Code)."""
        self.following = True
        self.scrolling = False
        self.unread = 0
        self.scroll_end(animate=False, immediate=True)

    def on_resize(self):
        self.stick()

    def clear(self):
        self.following, self.held = True, 0.0
        self.remove_children()
        self.calls = {}
        self.started = None
        self.restarts = 0
        self.unread = 0
        self.shown = set()
        self.echoes = {}
        self.drafting = None

    def blocks(self, record, source):
        """The widgets a record adds; a tool or ask record changes its call's
        block in place instead."""
        kind = record.get("type")
        if kind in ("user", "answer"):
            if kind == "user" and not record.get("steer"):
                self.started, self.restarts = record.get("time"), 0
            if record.get("source") == "job":
                first = record["text"].strip().splitlines()[0]
                return [Plain(f"◈ {first}", classes="note")]
            inbox = record.get("inbox")
            if inbox is not None and inbox in self.shown:
                return []
            self.shown.add(inbox)
            if record.get("continue"):
                return [yours("/continue")]
            label = who(record.get("source"), source)
            marks = [] if label == "you" else [label]
            if record.get("steer") and kind == "user":
                marks.append("steer")
            return [yours(record["text"], " · ".join(marks) or None)]
        if kind == "assistant":
            content = record["message"].get("content")
            made = []
            if content:
                made.append(message("●", Markdown(content), "zero"))
            for call in record["message"].get("tool_calls") or []:
                block = Fold(header_of(call), classes="tool")
                block.open = self.folds_open
                self.calls[call["id"]] = block
                made.append(block)
            return made
        if kind == "tool":
            block = self.calls.get(record.get("call"))
            if block is not None:
                block.finish(record["result"])
                return []
            return [Plain(record["result"][:200], classes="note")]
        if kind == "ask":
            block = self.calls.get(record.get("call"))
            if block is not None:
                block.waiting()
            return []
        if kind == "note":
            self.restarts += record.get("kind") == "restarted"
            if record.get("kind") == "switch":
                return [Plain(f"◈ {render.switch_text(record)}", classes="note")]
            return [Plain(f"◈ {record['text']}", classes="note")]
        if kind == "turn_end":
            # An answer needs no line of its own; a turn that ended without
            # one says how
            outcome = record["outcome"]
            if outcome == "answered":
                return []
            elapsed = duration(record, self.started, self.restarts)
            why = record.get("detail") or ""
            if record.get("error"):
                why = record["error"].strip().splitlines()[-1]
            words = f"{outcome}: {why}" if why else outcome
            return [Plain(f"✗ {words} · {elapsed}", classes="note failed")]
        return []

    def draft(self, draft):
        """The reply as it streams in: ZERO's message, growing, with a mark at
        its end. Only its text shows; reasoning shows as time on the progress
        line, a command once its record lands."""
        text = draft.get("text") or ""
        if not text:
            return
        shown = text + " ▍"
        if self.drafting is None:
            body = Markdown(shown)
            self.drafting = [message("●", body, "zero draft"), body, shown]
            self.mount(self.drafting[0])
        else:
            self.drafting[2] = shown
            # After the refresh, so a Markdown still mounting has taken its
            # first text and can't put it back over this one
            self.call_after_refresh(self.show_draft)
        self.call_after_refresh(self.stick)

    async def show_draft(self):
        if self.drafting is not None:
            _, body, shown = self.drafting
            if body.source != shown:
                # The update mounts its paragraphs over a few frames; the
                # place is kept once they are in
                await body.update(shown)
                self.stick()

    def undraft(self):
        """The next record replaces the draft: the reply itself, a retry, a
        stop."""
        if self.drafting is not None:
            self.drafting[0].remove()
            self.drafting = None

    def add(self, record, source, follow=True):
        self.undraft()
        widgets = self.blocks(record, source)
        if not widgets:
            return
        self.mount(*widgets)
        if not follow:
            # A session opening: its end shows
            self.following = True
        elif not self.following:
            self.unread += 1
        self.call_after_refresh(self.stick)

    def sent(self, text, inbox, mark=None):
        """A message of the user's, shown as it is sent from here."""
        self.shown.add(inbox)
        self.echoes[inbox] = yours(text, mark)
        self.mount(self.echoes[inbox])
        # Sending takes the user to the end
        self.following = True
        self.call_after_refresh(self.stick)

    def unsend(self, inbox):
        """A message a stop gave back: it never reached ZERO."""
        echo = self.echoes.pop(inbox, None)
        if echo is not None:
            echo.remove()

    def line(self, text, classes="note"):
        self.mount(Plain(text, classes=classes))
        if not self.following:
            self.unread += 1
        self.call_after_refresh(self.stick)


def header_of(call):
    """`$ command` for bash; the tool's name and arguments otherwise."""
    name = call["function"]["name"]
    command = render.command_of(call)
    if name == "bash":
        return f"$ {command}"
    if name == "propose_plan":
        return f"plan: {command}"
    return f"{name}: {command}"


class MessageBox(TextArea):
    """The input: it wraps and grows to a few lines. Enter sends; Shift+Enter
    or Ctrl+Enter (where the terminal tells them from Enter) or a \\ just
    before Enter starts a new line instead; the user's keys where they set
    others (keybindings.py)."""

    class Submitted(Message):
        def __init__(self, box, value):
            super().__init__()
            self.input = box
            self.value = value

    # Ctrl+A selects all, as in a desktop app; Home goes to the line's
    # start
    BINDINGS = [Binding("ctrl+a", "select_all", show=False)]

    def __init__(self, **options):
        super().__init__(soft_wrap=True, highlight_cursor_line=False, **options)

    def action_paste(self):
        """The desktop's clipboard; without a tool that reads it, what
        was copied in ZERO, as Textual's own paste."""
        if self.read_only:
            return
        text = clipboard.paste()
        if text is None:
            return super().action_paste()
        if result := self._replace_via_keyboard(text, *self.selection):
            self.move_cursor(result.end_location)

    @property
    def value(self):
        return self.text

    @value.setter
    def value(self, text):
        self.text = text
        self.move_cursor(self.document.end)

    def _on_key(self, event):
        # The ask's box: with nothing typed, ↑/↓ and a choice's number
        # pick among its choices
        pick = getattr(self, "pick", None)
        if pick is not None and not self.text and pick(event.key):
            event.stop()
            event.prevent_default()
            return
        mine = getattr(self.app, "keys", None) or keybindings.current()
        if event.key not in mine["new_line"] and event.key not in mine["send"]:
            # TextArea's own handler takes it
            return
        event.stop()
        event.prevent_default()
        row, column = self.cursor_location
        before = self.document.get_line(row)[:column]
        if event.key in mine["new_line"]:
            self.insert("\n")
        elif before.endswith("\\"):
            self.replace("\n", (row, column - 1), (row, column))
        else:
            self.post_message(self.Submitted(self, self.text))


def placeholder(current):
    """The input's hint, with the user's keybindings."""
    def key(id):
        return keybindings.pretty(current[id][0])

    return (f"Message ZERO   {key('new_line')} new line · {key('follow_up')} "
            f"after the answer · / commands · {key('switch_view')} sessions")
