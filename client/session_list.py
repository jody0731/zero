"""The session list as a face shows it: who needs you, the groups by folder,
each row's state and mark, which session a bare `zero` opens."""

import os
import time


# The attention line names this many sessions, then "+N more"
ATTENTION = 3


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
    asks.sort(key=lambda row: row["ask"]["created"])
    news.sort(key=lambda row: row["updated"])
    return asks + news


def attention_line(sessions, current=None, home=None):
    """The line above the input: what needs you elsewhere, or '' for none."""
    rows = needs_you(sessions, current)
    if not rows:
        return ""
    parts = []
    for row in rows[:ATTENTION]:
        place = brief(row["workspace"], 24, home)
        if row.get("ask") is not None:
            parts.append(f"? {row['title']} ({place}) waits for you")
        else:
            parts.append(f"● {row['title']} ({place}) has news")
    if len(rows) > ATTENTION:
        parts.append(f"+{len(rows) - ATTENTION} more")
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
        key=lambda row: row["ask"]["created"],
    )
    rest = sorted(
        (row for row in rows if row.get("ask") is None),
        key=lambda row: -row["updated"],
    )
    return waiting, rest


def opening(cwd, last, sessions):
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
    return max(here, key=lambda row: row["updated"])["id"], False


def short(path, home=None):
    """A folder as the user would write it: ~ for their home."""
    home = home or os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home.rstrip("/") + "/"):
        return "~" + path[len(home.rstrip("/")):]
    return path


def brief(path, width=28, home=None):
    """A folder short enough for a line: ~ for home, then its end kept."""
    text = short(path, home)
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
        return f"waiting {ago(now - row['ask']['created'])}"
    state = row["state"]
    if state in ("idle", "failed"):
        word = "done" if row.get("unseen") else state
        return f"{word} {ago(now - row['updated'])} ago"
    return f"{state} {ago(now - row['updated'])}"


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
