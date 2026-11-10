"""A session's use, from its log: working time and the tokens of its model
calls, by kind and as weighted tokens, the unit ZERO counts and estimates in
(0.6.8): a new input token counts 1, one read from the cache and an output
token what the model's price makes them against it (`token_weights`), every
token 1 for a model with no price. The price is the log's settings' (`price`,
and for a call the fallback model made its own `fallback_price`, or the
session's from before 0.6.7.1, when the fallback was fixed at the session's
build). Dollars are the user's unit: a round's line on the screens shows
them (rounds/service.py), and nothing the model reads does.

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


def token_weights(price):
    """What a token read from the cache and an output token count, against a
    new input token's 1: (cache, output), from `price` (USD per 1M tokens:
    cache hit, miss, output). None with no price, or none for new input:
    every token then counts 1."""
    if not price or not price[1]:
        return None
    hit, miss, out = price
    return hit / miss, out / miss


def own_price(record, price, fallback_price=None):
    """The price of the model that made an assistant record's call: when it
    ran on the fallback model (core/loop.py names it), the price the record
    carries, else `fallback_price` (an older log's); otherwise `price`."""
    if record.get("fallback"):
        return record.get("fallback_price", fallback_price)
    return price


def call_weighted_tokens(prompt, cached, completion, price):
    """One model call's tokens, weighted by `price`."""
    cache, output = token_weights(price) or (1.0, 1.0)
    prompt, cached, completion = prompt or 0, cached or 0, completion or 0
    return cached * cache + (prompt - cached) + completion * output


def record_weighted_tokens(record, price, fallback_price=None):
    """An assistant record's weighted tokens, at its own model's weights (the
    fallback's for a call it made, as record_dollars prices it)."""
    return call_weighted_tokens(
        record.get("prompt_tokens"), record.get("cached_tokens"),
        record.get("completion_tokens"), own_price(record, price, fallback_price))


def turn_weighted_tokens(records, price, fallback_price=None):
    """A turn's model calls in weighted tokens, each at its own model's."""
    return sum(record_weighted_tokens(r, price, fallback_price) for r in records
               if r["type"] == "assistant")


def context_tokens(records):
    """The context's size as the model's server last counted it: the tokens
    the latest model call read; None with no call since the context was
    last written whole (a new session, a compaction, a model switch). New
    code rewrites the system message around the same conversation, and the
    count holds near enough. Counted, never estimated: what came since that
    call isn't in it."""
    found = None
    for record in records:
        if record["type"] in history.SYSTEM_RECORDS:
            if not (record["type"] == "system" and record.get("why") == "code"):
                found = None
        elif record["type"] == "assistant" and record.get("prompt_tokens"):
            found = record["prompt_tokens"]
    return found


def reread_weighted_tokens(tokens, price):
    """What reading `tokens` again from the cache counts at `price`: a
    step's cost for a context of that size, before anything new."""
    cache, _ = token_weights(price) or (1.0, 1.0)
    return tokens * cache


def turn_reread_weighted_tokens(records, price, fallback_price=None):
    """The part of a turn's weighted tokens that read the conversation
    again: each call's tokens from the cache, at its own model's weight."""
    return sum(
        reread_weighted_tokens(
            r.get("cached_tokens") or 0, own_price(r, price, fallback_price))
        for r in records if r["type"] == "assistant")


def dollars_of(weighted_tokens, price):
    """Weighted tokens as dollars at `price`, for the user's screens; nothing
    with no price."""
    if token_weights(price) is None:
        return 0.0
    return weighted_tokens * price[1] / 1e6


def call_dollars(prompt, cached, completion, price):
    """One model call's cost at `price`; nothing without one."""
    if not price:
        return 0.0
    hit, miss, out = price
    prompt, cached, completion = prompt or 0, cached or 0, completion or 0
    return (cached * hit + (prompt - cached) * miss + completion * out) / 1e6


def record_dollars(record, price, fallback_price=None):
    """An assistant record's cost at its own model's price (USD per 1M
    tokens: cache hit, miss, output)."""
    return call_dollars(record.get("prompt_tokens"), record.get("cached_tokens"),
                        record.get("completion_tokens"),
                        own_price(record, price, fallback_price))


def turn_dollars(records, price, fallback_price=None):
    """What a turn's model calls cost, each at its own model's price."""
    return sum(record_dollars(r, price, fallback_price) for r in records
               if r["type"] == "assistant")


@dataclass
class Use:
    """Working seconds, and the tokens of the model calls: weighted, and by
    kind."""

    seconds: float = 0.0
    weighted_tokens: float = 0.0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0

    def __add__(self, other):
        return Use(*(getattr(self, f.name) + getattr(other, f.name)
                     for f in fields(self)))

    def add_call(self, record, price, fallback_price=None):
        self.weighted_tokens += record_weighted_tokens(
            record, price, fallback_price)
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
    """Dollars as the user's screens show them: to the cent, and an amount
    under a cent to its first two figures ($0.0017), so a small use isn't
    $0.00."""
    if amount <= 0 or amount >= 0.01:
        return f"${amount:,.2f}"
    places = 1 - math.floor(math.log10(amount))
    return f"${amount:.{places}f}"


def tokens_text(weighted_tokens):
    """Weighted tokens as the model reads them: "33,064 weighted tokens"."""
    return f"{round(weighted_tokens):,} weighted tokens"


def estimate_text(minutes, weighted_tokens):
    """An estimate a means was started with: "20 min and 150,000 weighted
    tokens"; minutes alone for one from before 0.6.8, made in dollars."""
    if weighted_tokens is None:
        return f"{minutes:g} min"
    return f"{minutes:g} min and {tokens_text(weighted_tokens)}"


def past_estimate(use, minutes, weighted_tokens):
    """Whether a use passes its estimate, in minutes or weighted tokens (an
    estimate from before 0.6.8 has minutes alone)."""
    if use.minutes() > minutes:
        return True
    return weighted_tokens is not None and use.weighted_tokens > weighted_tokens


def describe(use):
    """A use as the model reads it: "12 min, 210,000 tokens in (92% from the
    cache) and 3,100 out: 33,064 weighted tokens"; under a minute in seconds,
    under ten minutes to a tenth, so a use just past its estimate reads past
    it."""
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
    if not use.prompt_tokens:
        return f"{took}, {tokens_text(use.weighted_tokens)}"
    share = round(100 * use.cached_tokens / use.prompt_tokens)
    return (f"{took}, {use.prompt_tokens:,} tokens in ({share}% from the cache) "
            f"and {use.completion_tokens:,} out: "
            f"{tokens_text(use.weighted_tokens)}")


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

    def context(self):
        """The context's size now and what a step counts for reading it
        again: (tokens, weighted tokens); None before a call has read the
        context as it stands."""
        session = self.session_of()
        tokens = context_tokens(session.log.branch())
        if tokens is None:
            return None
        return tokens, reread_weighted_tokens(
            tokens, session.settings.get("price"))
