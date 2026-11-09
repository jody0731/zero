"""A session's use, from its log: working time, the tokens of its model calls,
and their dollars at the model's price, which the log's settings carry
(`price`, and for a call the fallback model made its own `fallback_price`,
or the session's from before 0.6.7.1, when the fallback was fixed at the
session's build; a model with no price spends nothing, as does a fallback's
call in a log from before 0.6.7, which named no price for it).

A whole log counts, every branch: a part a rewind left behind cost what it
cost. A fork's copied records are its parent's, so its use starts after
them. The working time is each ended turn's own (`agent_seconds`), and the
open turn's so far, its waits on the user left out.
"""

import json
import math
import threading
from dataclasses import dataclass, fields
from datetime import datetime

from core import history


def call_dollars(prompt, cached, completion, price):
    """One model call's cost at `price`; nothing without one."""
    if not price:
        return 0.0
    hit, miss, out = price
    prompt, cached, completion = prompt or 0, cached or 0, completion or 0
    return (cached * hit + (prompt - cached) * miss + completion * out) / 1e6


def record_dollars(record, price, fallback_price=None):
    """An assistant record's cost: when it ran on the fallback model
    (core/loop.py names it), at the price the record carries, else at
    `fallback_price` (an older log's); otherwise at `price` (USD per 1M
    tokens: cache hit, miss, output)."""
    if record.get("fallback"):
        own = record.get("fallback_price", fallback_price)
    else:
        own = price
    return call_dollars(record.get("prompt_tokens"), record.get("cached_tokens"),
                        record.get("completion_tokens"), own)


def turn_dollars(records, price, fallback_price=None):
    """What a turn's model calls cost, each at its own model's price."""
    return sum(record_dollars(r, price, fallback_price) for r in records
               if r["type"] == "assistant")


@dataclass
class Use:
    """Working seconds, dollars, and the tokens of the model calls."""

    seconds: float = 0.0
    dollars: float = 0.0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0

    def __add__(self, other):
        return Use(*(getattr(self, f.name) + getattr(other, f.name)
                     for f in fields(self)))

    def add_call(self, record, price, fallback_price=None):
        self.dollars += record_dollars(record, price, fallback_price)
        self.prompt_tokens += record.get("prompt_tokens") or 0
        self.cached_tokens += record.get("cached_tokens") or 0
        self.completion_tokens += record.get("completion_tokens") or 0

    def minutes(self):
        return self.seconds / 60


def seconds_between(start, end):
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def open_seconds(records, now=None):
    """The open turn's working time so far, from its records: from its first
    record to now, each wait on the user's answer left out."""
    if not records:
        return 0.0
    now = now or datetime.now().astimezone().isoformat()
    waited = sum(r.get("waited", 0) for r in records if r["type"] == "answer")
    return max(seconds_between(records[0]["time"], now) - waited, 0.0)


def turn_use(records, price, fallback_price=None, now=None):
    """The open turn's use (history.unfinished_turn's records)."""
    use = Use(seconds=open_seconds(records, now))
    for record in records:
        if record["type"] == "assistant":
            use.add_call(record, price, fallback_price)
    return use


class LogUse:
    """A log's use in all, read as it grows: each read takes only the whole
    lines added since the last. A fork's copied records (up to its header's
    `forked_from` seq) are its parent's and don't count."""

    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.ended = Use()
        self.price = self.fallback_price = None
        # The open turn's records: its time and calls count until it ends
        self.open = []
        self.copied_to = 0
        self.lock = threading.Lock()

    def take(self, record):
        if record["type"] == "session":
            self.copied_to = (record.get("forked_from") or {}).get("seq", 0)
        if record.get("settings") and "price" in record["settings"]:
            self.price = record["settings"]["price"]
            self.fallback_price = record["settings"].get("fallback_price")
        if record["seq"] <= self.copied_to:
            return
        if record["type"] == "user" and not record.get("steer"):
            self.open = [record]
        elif record["type"] == "turn_end":
            for r in self.open:
                if r["type"] == "assistant":
                    self.ended.add_call(r, self.price, self.fallback_price)
            self.ended.seconds += record.get("agent_seconds") or 0.0
            self.open = []
        elif self.open:
            self.open.append(record)

    def read(self, now=None):
        """The use so far: the ended turns', and the open one's up to now."""
        with self.lock:
            try:
                with open(self.path, "rb") as f:
                    f.seek(self.offset)
                    data = f.read()
            except FileNotFoundError:
                data = b""
            end = data.rfind(b"\n") + 1
            for line in data[:end].split(b"\n"):
                if line.strip():
                    self.take(json.loads(line.decode("utf-8")))
            self.offset += end
            return self.ended + turn_use(self.open, self.price,
                                         self.fallback_price, now)


# Each log's reader, by path, kept while ZERO runs: a later read takes only
# what was added
READERS = {}
READERS_LOCK = threading.Lock()


def log_use(path, now=None):
    """The use in all of the log at `path`."""
    with READERS_LOCK:
        reader = READERS.setdefault(str(path), LogUse(path))
    return reader.read(now)


def dollars_text(amount):
    """Dollars as the model reads them: to the cent, and an amount under a
    cent to its first two figures ($0.0017), so a small use isn't $0.00."""
    if amount <= 0 or amount >= 0.01:
        return f"${amount:,.2f}"
    places = 1 - math.floor(math.log10(amount))
    return f"${amount:.{places}f}"


def describe(use):
    """A use as the model reads it: "12 min, 210,000 tokens in (92% from the
    cache) and 3,100 out, $0.08"; under a minute in seconds, under ten
    minutes to a tenth, so a use just past its estimate reads past it."""
    minutes = round(use.minutes())
    if use.seconds < 60:
        took = f"{max(round(use.seconds), 0)} s"
    elif use.minutes() < 10:
        took = f"{use.minutes():.1f} min"
    elif minutes < 60:
        took = f"{minutes} min"
    else:
        hours, rest = divmod(minutes, 60)
        took = f"{hours} h {rest} min" if rest else f"{hours} h"
    tokens = ""
    if use.prompt_tokens:
        share = round(100 * use.cached_tokens / use.prompt_tokens)
        tokens = (f", {use.prompt_tokens:,} tokens in ({share}% from the cache) "
                  f"and {use.completion_tokens:,} out")
    return f"{took}{tokens}, {dollars_text(use.dollars)}"


class SessionUse:
    """What one session's `usage` tool reads: this turn's and the session's
    own use, from its log; and, where a server hands one in, its subagents'
    (`subagents()`, the subagents' service: each with its id, title, state,
    use and the estimate it was started with)."""

    def __init__(self, session_of):
        # The session, a callable: a switch builds it again
        self.session_of = session_of
        self.subagents = None

    def turn(self):
        session = self.session_of()
        records = history.unfinished_turn(session.log.branch())
        return turn_use(records, session.settings.get("price"),
                        session.settings.get("fallback_price"))

    def own(self):
        return log_use(self.session_of().log.path)
