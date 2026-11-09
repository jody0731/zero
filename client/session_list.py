"""The session list as a face shows it: who needs you, the groups by folder,
each row's state and mark, which session a bare `zero` opens."""

import os
import time


# The attention line names this many sessions, then "+N more"
ATTENTION_SESSIONS = 3


def needs_you(sessions, current=None):
    """The other sessions that need the user, in Alt+↓'s order: open asks
    first, the oldest first; then news (a turn ended since they last
    looked), the oldest first. Archived sessions don't count."""
    asks, news = [], []
    for row in sessions:
        if row["id"] == current or row.get("archived"):
            continue
        if row.get("ask") is not None:
            asks.append(row)
        elif row.get("unseen"):
            news.append(row)
    asks.sort(key=lambda row: row["ask"]["created_at"])
    news.sort(key=lambda row: row["updated_at"])
    return asks + news


def attention_line(sessions, current=None, home=None):
    """The line above the input: what needs you elsewhere, or '' for none."""
    rows = needs_you(sessions, current)
    if not rows:
        return ""
    parts = []
    for row in rows[:ATTENTION_SESSIONS]:
        place = short_path(row["workspace"], 24, home)
        if row.get("ask") is not None:
            parts.append(f"? {row['title']} ({place}) waits for you")
        else:
            parts.append(f"● {row['title']} ({place}) has news")
    if len(rows) > ATTENTION_SESSIONS:
        parts.append(f"+{len(rows) - ATTENTION_SESSIONS} more")
    return "   ".join(parts) + "   Alt+↓"


def groups(sessions, typed=""):
    """The list (Ctrl+O): (waiting for you, the rest), each filtered by what
    is typed, matched in the title or workspace; the rest by last activity."""
    typed = typed.strip().lower()
    rows = [
        row for row in sessions
        if not typed or typed in row["title"].lower()
        or typed in row["workspace"].lower()
    ]
    waiting = sorted(
        (row for row in rows if row.get("ask") is not None),
        key=lambda row: row["ask"]["created_at"],
    )
    rest = sorted(
        (row for row in rows if row.get("ask") is None),
        key=lambda row: -row["updated_at"],
    )
    return waiting, rest


def session_to_open(cwd, last, sessions):
    """Which session a bare `zero` opens: at /, the last one
    the user worked in, else the list; anywhere else, the latest session
    whose workspace is exactly that folder, else a new one made on the first
    message. Returns (session id or None, whether to show the list)."""
    live = [row for row in sessions if not row.get("archived")]
    if cwd == "/":
        if last is not None and any(row["id"] == last for row in live):
            return last, False
        return None, bool(live)
    here = [row for row in live if row["workspace"] == cwd]
    if not here:
        return None, False
    return max(here, key=lambda row: row["updated_at"])["id"], False


def tilde_path(path, home=None):
    """A folder as the user would write it: ~ for their home."""
    home = home or os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home.rstrip("/") + "/"):
        return "~" + path[len(home.rstrip("/")):]
    return path


def short_path(path, width=28, home=None):
    """A folder short enough for a line: ~ for home, then its end kept."""
    text = tilde_path(path, home)
    if len(text) <= width:
        return text
    return "…" + text[-(width - 1):]


def ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400 * 2:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


def row_state(row, now=None):
    """A list row's state and how long: 'waiting 12 min', 'running 40 s'."""
    now = time.time() if now is None else now
    if row.get("ask") is not None:
        return f"waiting {ago(now - row['ask']['created_at'])}"
    state = row["state"]
    if state in ("idle", "failed"):
        word = "done" if row.get("unseen") else state
        return f"{word} {ago(now - row['updated_at'])} ago"
    return f"{state} {ago(now - row['updated_at'])}"


def row_title(row):
    """A list row's title, a subagent's and a fork's marked as one."""
    if row.get("started_by") == "zero":
        return f"{row['title']} (subagent)"
    return f"{row['title']} (fork)" if row.get("parent") else row["title"]


def row_mark(row):
    if row.get("ask") is not None:
        return "?"
    if row.get("unseen"):
        return "●"
    if row["state"] == "running":
        return "▶"
    if row["state"] == "failed":
        return "✗"
    return " "
