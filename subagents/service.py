"""The subagents' service in the server (server/services.py): a subagent
started, its task queued, messages between sessions, the session tree, a
stop, its use against its estimate, and the harness's word on what a
subagent didn't send.

A subagent is a session of its own under the one that started it (the
kernel's `parent`, `started_by` zero), as every harness read in source has
it (Codex, OpenCode, DSH, OpenClaw): a fork of the parent's finished turns
(6.6's fork, mid-turn) or a new session with its task alone. Its task is its
first message, a notice from "session", which asks it, last, to report back
with send_message when it has finished: the subagent writes its own report,
since it alone knows all of its results. The harness passes up what it
didn't send, as a notice from "subagent": a turn's answer whose last call
wasn't a message to its parent that went through (DSH's settlement rule), a
turn it couldn't finish (a crash, a failure, a stop), both to its parent; a
turn parked on a question, to the top session of its tree. Each notice is
written once by its id, so a crash between the turn's end and the notice is
repaired at the next start (the runtime tells the services again). A notice
starts a turn in a session at rest, joins a running turn, and waits while an
ask waits; it is never the reply to an ask, so a message from another
session never carries approval.

A message reaches any session by its id: the parent knows its subagent's
from start_subagent, the subagent its parent's from its task. A session
tree is a session the user started and the subagents under it, through
this table's `parent`; a fork the user makes of a subagent isn't in it, and
starts a tree of its own. Depth counts levels below the session the user
started (`max_subagent_depth`, read from the session's limits at each use,
as every limit is; 0: none); breadth has no cap. A stop ends one turn:
the subagents below it go on.
"""

import json
import time
import uuid

from core import history
from server.services import Refused, Service
from subagents import tool as subtools
from subagents.store import SCHEMA, SubagentStore
from usage.spend import describe, log_use

# What send_message returns when a message went through
SENT = "Sent to session"


class SessionSubagents:
    """What one session's tools and hooks get as `session_subagents`: the
    server's subagents, bound to the session."""

    def __init__(self, service, sid, session_of):
        self.service = service
        self.sid = sid
        # The session, a callable: a switch builds it again
        self.session_of = session_of
        self._depth = None

    def depth(self):
        """Its level below the session the user started; it never changes."""
        if self._depth is None:
            self._depth = self.service.depth(self.sid)
        return self._depth

    def max_depth(self):
        """How deep subagents go, from the session's limits as they stand."""
        limits = self.session_of().tool_context.get("limits")
        if limits is None:
            return self.service.max_depth()
        return limits["max_subagent_depth"]

    def can_start(self):
        """Whether start_subagent is offered: at every level while subagents
        are on, so a fork's tools stay its parent's (and its cache whole); a
        call from the last level is refused with the fact."""
        return self.max_depth() > 0

    def start(self, task, start_from, title, minutes, dollars):
        """(the new subagent's id, None), or (None, why it wasn't
        started)."""
        deepest = self.max_depth()
        if deepest <= 0:
            return None, ("Not started: subagents are off (Subagents depth "
                          "is 0 in ZERO's settings).")
        if self.depth() >= deepest:
            levels = "level" if deepest == 1 else "levels"
            return None, (f"Not started: this session is at level "
                          f"{self.depth()}, and subagents go {deepest} "
                          f"{levels} below a session the user started.")
        return self.service.start(self.sid, self.session_of(), task, start_from,
                                  title, minutes, dollars, self.depth() + 1)

    def send(self, to, text):
        return self.service.send(self.sid, to, text)

    def tree(self):
        return self.service.tree(self.sid)

    def look(self, id):
        return self.service.look(self.sid, id)

    def stop(self, id):
        return self.service.stop(self.sid, id)

    def check_estimates(self):
        self.service.check_estimates(self.sid)


class SubagentsService(Service):
    name = "subagents"
    schema = SCHEMA
    tables = ("subagents",)

    def __init__(self, clock=time.time):
        self.clock = clock

    def attach(self, runtime):
        super().attach(runtime)
        self.store = SubagentStore(runtime.store)

    def session_built(self, sid, session):
        """The session's tools start subagents and message its tree through
        the server; its use counts its subagents'; its judge reads the
        user's words in the whole tree."""
        session.tool_context["session_subagents"] = SessionSubagents(
            self, sid, lambda: session)
        use = session.tool_context.get("session_use")
        if use is not None:
            use.subagents = lambda: self.children_use(sid)
        session.tool_context["tree_user_messages"] = (
            lambda: self.tree_user_messages(sid))

    def max_depth(self):
        """How deep subagents go, from the server's settings as they stand
        (config.toml read again when it changes), for a session without
        limits of its own: a change reaches every session, open ones too, at
        its next model call; 0, none."""
        return self.runtime.setting("max_subagent_depth")

    # The tree

    def depth(self, sid):
        row = self.store.subagent(sid)
        return row["depth"] if row else 0

    def parent_of(self, sid):
        """The session that started this subagent, while it still exists;
        None for a session the user started."""
        row = self.store.subagent(sid)
        if row is None or self.runtime.store.session(row["parent"]) is None:
            return None
        return row["parent"]

    def root(self, sid):
        """The session the user started at the top of this one's tree."""
        while True:
            parent = self.parent_of(sid)
            if parent is None:
                return sid
            sid = parent

    def below(self, sid):
        """Every subagent under the session, at any depth, in tree order."""
        found = []
        for row in self.store.children(sid):
            if self.runtime.store.session(row["session"]) is None:
                continue
            found.append(row)
            found += self.below(row["session"])
        return found

    def members(self, sid):
        """The session tree this one is in: its root first, then the
        subagents under it, in tree order."""
        root = self.root(sid)
        return [root, *(row["session"] for row in self.below(root))]

    def path(self, sid):
        return self.runtime.paths.sessions / f"{sid}.jsonl"

    def subtree_use(self, sid):
        """The use of the session and every subagent under it."""
        use = log_use(self.path(sid))
        for row in self.below(sid):
            use = use + log_use(self.path(row["session"]))
        return use

    def children_use(self, sid):
        """The subagents this session started, each with its use (its own
        subagents' counted in) and estimate: the `usage` tool's list."""
        found = []
        for row in self.store.children(sid):
            info = self.runtime.store.session(row["session"])
            if info is None:
                continue
            found.append({
                "id": row["session"], "title": info["title"],
                "state": self.state(row["session"])[0],
                "use": self.subtree_use(row["session"]),
                "estimate": (row["estimate_minutes"], row["estimate_dollars"]),
            })
        return found

    def busy(self, sid):
        """Whether the session is at work: a turn running, a message waiting
        to start one, or a question waiting for the user."""
        row = self.runtime.store.session(sid)
        return (row["state"] in ("running", "waiting")
                or self.runtime.store.open_ask(sid) is not None
                or self.runtime.turn_busy(sid))

    def state(self, sid):
        """(its state, its last turn's outcome or None)."""
        row = self.runtime.store.session(sid)
        if self.runtime.store.open_ask(sid) is not None:
            return "waiting for the user's answer", None
        if row["state"] == "running" or self.runtime.turn_busy(sid):
            return "running", None
        end = last_end(self.runtime.log(sid).branch())
        outcome = end["outcome"] if end else None
        return ("failed" if row["state"] == "failed" else "idle"), outcome

    def entry(self, sid, this):
        """A session of the tree as `subagents` lists it."""
        info = self.runtime.store.session(sid)
        row = self.store.subagent(sid)
        state, outcome = self.state(sid)
        return {
            "id": sid, "title": info["title"], "this": sid == this,
            "depth": row["depth"] if row else 0,
            "started_by": "zero" if row else "user",
            "state": state, "outcome": outcome,
            "use": self.subtree_use(sid),
            "estimate": ((row["estimate_minutes"], row["estimate_dollars"])
                         if row else None),
        }

    def tree(self, sid):
        return [self.entry(member, sid) for member in self.members(sid)]

    def look(self, sid, id):
        if id not in self.members(sid):
            return f"No session {id} in this session tree."
        found = self.entry(id, sid)
        end = last_end(self.runtime.log(id).branch())
        found["ended"] = end is not None
        found["answer"] = end.get("answer") if end else None
        return found

    def tree_user_messages(self, sid):
        """The user's words in the tree's other sessions, oldest first (each
        session's own records: a subagent's fork copies its parent's), and
        where this session's own records start: the judge's goal."""
        found = []
        own_from = 0
        for member in self.members(sid):
            records = self.runtime.log(member).branch()
            start = copied_to(records) if self.store.subagent(member) else 0
            if member == sid:
                own_from = start
                continue
            found += [(r["time"], r["text"]) for r in records
                      if r["seq"] > start and r["type"] == "user"
                      and not history.is_notice(r)]
        found.sort()
        return [text for _, text in found], own_from

    # What a subagent does

    def start(self, sid, session, task, start_from, title, minutes, dollars,
              depth):
        """A subagent of session `sid`: its session, its row and its task's
        notice; (its id, None), or (None, why it wasn't started)."""
        parent = self.runtime.store.session(sid)
        try:
            if start_from == "fork":
                child = self.runtime.fork_finished(sid, title, "zero")
            else:
                child = self.runtime.new_child(sid, title, "zero")
        except Refused as error:
            return None, f"Not started: {error}."
        text = subtools.TASK.format(parent=sid, title=parent["title"], task=task)
        self.store.add({
            "session": child, "parent": sid, "start_from": start_from,
            "depth": depth, "started_from_seq": session.log.last_seq,
            "estimate_minutes": minutes, "estimate_dollars": dollars,
        }, (f"subagent-task:{child}", child, subtools.ACROSS, {"text": text}))
        self.runtime.wake()
        return child, None

    def send(self, sid, to, text):
        """A message to any other session, by its id, as a notice. A
        subagent's message to its parent carries the record the parent
        started it from, so one reaching a part a rewind left behind is
        marked (6.6)."""
        if to == sid:
            return "Error: that is this session."
        if self.runtime.store.session(to) is None:
            return f"Not sent: no session {to}."
        info = self.runtime.store.session(sid)
        payload = {"text": subtools.MESSAGE.format(id=sid, title=info["title"],
                                                   text=text)}
        row = self.store.subagent(sid)
        if row is not None and row["parent"] == to:
            payload["started_from_seq"] = row["started_from_seq"]
        self.store.notice(f"message:{uuid.uuid4()}", to, subtools.ACROSS, payload)
        self.runtime.wake()
        return f"{SENT} {to}."

    def stop(self, sid, id):
        """Stop the turn a subagent below this session runs; it alone."""
        if id not in [row["session"] for row in self.below(sid)]:
            return f"Not stopped: {id} isn't a subagent below this session."
        found = self.runtime.stop_turn(id, f"stop:{uuid.uuid4()}",
                                       f"session {sid}")
        if not found["stopped"]:
            return f"{id} had no turn running."
        return (f"Stopped {id}'s turn; its subagents go on, and a notice of "
                "the stop reaches its parent.")

    def turn_ended(self, sid, outcome):
        """A subagent's turn ended or parked on a question. The subagent
        reports with send_message; the harness passes up what it didn't send:
        a turn's answer whose last call wasn't a message to its parent that
        went through (once it rests: with a message already queued, the next
        round's or a steer, the next turn's answer is the one), a turn it
        couldn't finish (a crash, a failure, a stop), both to its parent; a
        question waiting for the user, to the top session of its tree. Kept
        once by its id."""
        row = self.store.subagent(sid)
        if row is None or self.runtime.store.session(row["parent"]) is None:
            return
        records = self.runtime.log(sid).branch()
        info = self.runtime.store.session(sid)
        found = {**row, "title": info["title"]}
        payload = {}
        if outcome == "waiting":
            ask = history.parked_ask(records)
            if ask is None:
                return
            key, to = ask["ask"], self.root(sid)
            payload["text"] = subtools.WAITING.format(
                id=sid, title=info["title"], parent=row["parent"], why=ask["why"])
        else:
            end = last_end(records)
            if end is None:
                return
            answered = end["outcome"] == "answered"
            if answered and (self.runtime.store.next_queued(sid) is not None
                             or sent_last(records, end, row["parent"])):
                return
            use = self.subtree_use(sid)
            key, to = end["seq"], row["parent"]
            payload["text"] = (
                subtools.unsent_text(found, use, end.get("answer")) if answered
                else subtools.report_text(found, end["outcome"], use,
                                          end.get("detail")))
            # The notice says how its use stands against its estimate
            if row["past_estimate_at"] is None and past(use, row):
                self.store.mark_past(sid, self.clock())
        if to == row["parent"]:
            payload["started_from_seq"] = row["started_from_seq"]
        self.store.notice(f"subagent:{sid}:{key}", to, subtools.UP, payload)
        self.runtime.wake()

    def check_estimates(self, sid):
        """The subagent's use, and each subagent's above it, against the
        estimate each was started with: past it, one notice to its parent."""
        while True:
            row = self.store.subagent(sid)
            if row is None or self.runtime.store.session(row["parent"]) is None:
                return
            if row["past_estimate_at"] is None:
                use = self.subtree_use(sid)
                if past(use, row):
                    info = self.runtime.store.session(sid)
                    text = subtools.PAST_ESTIMATE.format(
                        id=sid, title=info["title"],
                        use=describe(use),
                        estimate=subtools.estimate_text(
                            row["estimate_minutes"], row["estimate_dollars"]),
                    )
                    self.store.notice(
                        f"subagent:{sid}:past_estimate", row["parent"],
                        subtools.UP,
                        {"text": text, "started_from_seq": row["started_from_seq"]},
                    )
                    self.store.mark_past(sid, self.clock())
                    self.runtime.wake()
            sid = row["parent"]

    # The server's

    def settled(self, sid):
        """The subagents under the session still at work: a turn running, a
        message waiting to start one, or a question waiting for the user."""
        return {"subagents": [row["session"] for row in self.below(sid)
                              if self.busy(row["session"])]}

    def refuse_delete(self, sid):
        if self.settled(sid)["subagents"]:
            return ("a subagent under this session is at work; stop it, or "
                    "delete the session once it is at rest")
        return None


def last_end(records):
    """The session's own last turn_end, or None: a fork's copied turns are
    its parent's."""
    start = copied_to(records)
    return next((r for r in reversed(records)
                 if r["type"] == "turn_end" and r["seq"] > start), None)


def sent_last(records, end, parent):
    """Whether the turn `end` closes made, as its last call, a message to
    `parent` that went through (DSH's rule: any call after it voids it)."""
    start = max([copied_to(records)]
                + [r["seq"] for r in records
                   if r["type"] == "turn_end" and r["seq"] < end["seq"]])
    calls = [r for r in records
             if r["type"] == "tool" and start < r["seq"] < end["seq"]]
    if not calls:
        return False
    last = calls[-1]
    if (last.get("tool") != "send_message"
            or not str(last.get("result", "")).startswith(SENT)):
        return False
    try:
        args = json.loads(last.get("arguments") or "{}")
    except ValueError:
        return False
    return isinstance(args, dict) and str(args.get("session", "")).strip() == parent


def copied_to(records):
    """The last record a fork copied from its parent, else 0."""
    head = records[0] if records else {}
    return (head.get("forked_from") or {}).get("seq", 0)


def past(use, row):
    """Whether a use passes a subagent's estimate."""
    return (use.minutes() > row["estimate_minutes"]
            or use.dollars > row["estimate_dollars"])

