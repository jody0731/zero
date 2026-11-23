"""The subagents' service in the server (server/services.py): a subagent
started, its task queued, messages between sessions, the session tree, a
stop, its use against its estimate, and the harness's word on what a
subagent didn't send.

A subagent is a session of its own under the one that started it (the
kernel's `parent`, `started_by` zero), as every harness read in source has
it (Codex, OpenCode, DSH, OpenClaw): a fork of the parent's finished turns
(a fork, mid-turn) or a new session with its task alone. Its task is its
first message, a notice from "session", which asks it, last, to report back
with send_message when it has finished: the subagent writes its own report,
since it alone knows all of its results. The harness passes up what it
didn't send, as a notice from "subagent": a turn's answer whose last call
wasn't a message to its parent that went through (DSH's settlement rule),
unless every line of it, marks aside, is in what the turn did send there, a
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
import re
import time
import uuid

import paths
from core import attribution, entries, timeline
from server.services import Refused, Service
from server.runtime import Message
from components.subagents import tool as subtools
from components.subagents.store import ADDED_COLUMNS, SCHEMA, WORKFLOW, SubagentStore
from components.usage.spend import (
    describe, dollars_of, forget_log, log_use, passed_text, past_estimate,
)

# What send_message returns when a message went through
SENT = "Sent to session"
# Markdown's marks, which a report sent again as an answer may change
MARKS = re.compile(r"[`*_#>]")
# A workflow's subagent's title when its message names none: its task's
# first words, as many characters at most
TITLE_CHARS = 60


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

    def start(self, task, start_from, title, minutes, weighted_tokens):
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
                                  title, minutes, weighted_tokens,
                                  self.depth() + 1)

    def send(self, to, text, reply_to=None):
        return self.service.send(self.sid, to, text, reply_to=reply_to)

    def tree(self):
        return self.service.tree(self.sid)

    def look(self, id):
        return self.service.look(self.sid, id)

    def stop(self, id):
        return self.service.stop(self.sid, id)

    def zero_command(self):
        return self.service.zero_command()

    def check_estimates(self):
        self.service.check_estimates(self.sid)


class SubagentsService(Service):
    name = "subagents"
    schema = SCHEMA
    added_columns = ADDED_COLUMNS
    tables = ("subagents",)
    # A workflow, one of a session's commands calling ZERO, starts and
    # messages that session's subagents (server/server.py checks whose
    # command it is)
    workflow_messages = ("subagent/send",)

    def __init__(self, clock=time.time, zero_command=None):
        """`zero_command`: how a command runs this ZERO, as start_subagent
        tells it; None: found at first use (paths.zero_command)."""
        self.clock = clock
        self._zero_command = zero_command

    def zero_command(self):
        if self._zero_command is None:
            self._zero_command = paths.zero_command()
        return self._zero_command

    def attach(self, runtime):
        super().attach(runtime)
        self.store = SubagentStore(runtime.store)

    def unattended(self, sid):
        return self.store.subagent(sid) is not None

    def handoff_records(self, sid):
        row = self.store.subagent(sid)
        scope = row["parent"] if by_workflow(row) else sid
        records = list(self.runtime.read_branch(scope))
        for child in self.store.children(scope):
            if not by_workflow(child):
                continue
            branch = self.runtime.read_branch(child["session"])
            asks = {r.entry.id for r in branch if entries.has(r.entry, "ask")
                    and r.entry.get("notes", {}).get("handoff_scope") == scope}
            records.extend(r for r in branch
                           if (entries.has(r.entry, "ask") and r.entry.id in asks)
                           or (entries.has(r.entry, "answer")
                               and entries.linked_to(r.entry, "answers") in asks))
        return sorted(records, key=lambda record: record.time)

    def permission_records(self, sid):
        # A workflow's admission grant belongs to its parent, not this child.
        if by_workflow(self.store.subagent(sid)):
            return []
        own = self.runtime.read_branch(sid)
        records = self.handoff_records(sid)
        asks = {r.entry.id for r in records if entries.has(r.entry, "ask")
                and r.entry.get("notes", {}).get("handoff")}
        return [r for r in records if r not in own and (
            (entries.has(r.entry, "ask") and r.entry.id in asks)
            or (entries.has(r.entry, "answer")
                and entries.linked_to(r.entry, "answers") in asks)
            or r.entry.get("grant") in asks
        )]

    def workflow_handoff(self, sid, turn):
        row = self.store.subagent(sid)
        if not by_workflow(row) or turn.source != subtools.ACROSS:
            return None
        parent = self.runtime.store.session(row["parent"])
        if parent is None:
            raise Refused("the workflow's parent session no longer exists")
        if parent["mode"] != "default" or self.runtime.unattended(row["parent"]):
            return None
        return row["parent"]

    def session_built(self, sid, session):
        """The session's tools start subagents and message its tree through
        the server; its use counts its subagents'; its judge reads the
        user's words in the whole tree."""
        session.tool_context["handoff_records"] = lambda: self.handoff_records(sid)
        if by_workflow(self.store.subagent(sid)):
            session.tool_context["workflow_handoff"] = (
                lambda turn: self.workflow_handoff(sid, turn))
        session.tool_context["session_subagents"] = SessionSubagents(
            self, sid, lambda: session)
        use = session.tool_context.get("session_use")
        if use is not None:
            use.subagents = lambda: self.children_use(sid)
        session.tool_context["tree_user_messages"] = (
            lambda: self.tree_user_messages(sid))

    def messages(self):
        return {"subagent/send": self.workflow_send}

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

    def session_deleted(self, sid):
        """Its log's reader of what it used, let go of."""
        forget_log(self.path(sid))

    def subtree_use(self, sid):
        """The use of the session and every subagent under it."""
        def session_use(member):
            return log_use(self.path(member), read_legacy=lambda: (
                self.runtime.read_records(member)
            ))

        use = session_use(sid)
        for row in self.below(sid):
            use = use + session_use(row["session"])
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
                "estimate": estimate_of(row),
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
        end = last_end(self.runtime.read_branch(sid))
        outcome = end.entry.get("outcome") if end else None
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
            "by_workflow": by_workflow(row),
            "state": state, "outcome": outcome,
            "use": self.subtree_use(sid),
            "estimate": estimate_of(row),
        }

    def tree(self, sid):
        return [self.entry(member, sid) for member in self.members(sid)]

    def look(self, sid, id):
        if id not in self.members(sid):
            return f"No session {id} in this session tree."
        found = self.entry(id, sid)
        end = last_end(self.runtime.read_branch(id))
        found["ended"] = end is not None
        found["answer"] = end.entry.get("answer") if end else None
        return found

    def tree_user_messages(self, sid):
        """The user's words in the tree's other sessions, oldest first (each
        session's own records: a subagent's fork copies its parent's), and
        where this session's own records start: the judge's goal."""
        found = []
        own_from = 0
        for member in self.members(sid):
            records = self.runtime.read_branch(member)
            start = copied_to(records) if self.store.subagent(member) else 0
            if member == sid:
                own_from = start
                continue
            for record in records:
                entry = record.entry
                if (record.seq <= start or not entries.has(entry, "user_message")
                        or attribution.is_notice(entry) or entry.get("continue")):
                    continue
                if attribution.is_user_message(entry.origin, entry.get("source")):
                    found.append((record.time, entries.text(entry)))
        found.sort()
        return [text for _, text in found], own_from

    # What a subagent does

    def start(self, sid, session, task, start_from, title, minutes,
              weighted_tokens, depth):
        """A subagent of session `sid`: its session, its row and its task's
        notice; (its id, None), or (None, why it wasn't started). Its
        estimate is kept in weighted tokens, ZERO's unit, and in dollars at
        the session's price, the user's."""
        row = {
            "parent": sid, "start_from": start_from, "depth": depth,
            "started_from_seq": session.log.last_seq,
            "estimate_minutes": minutes,
            "estimate_weighted_tokens": weighted_tokens,
            "estimate_dollars": dollars_of(
                weighted_tokens, session.settings.get("price")),
        }
        return self.made(
            sid, title, start_from, row, task, "zero", parent_log=session.log,
        )

    def made(self, sid, title, start_from, row, task, source,
             mode=None, model=None, *, parent_log=None):
        """A subagent's session made under `sid` with `row` (its subagents
        row, less the session) and its task's notice, in one
        transaction; (its id, None), or (None, why it wasn't started)."""
        template = subtools.WORKFLOW_TASK if source == WORKFLOW else subtools.TASK

        def rows_beside(db, child, *, parent):
            text = template.format(parent=sid, title=parent["title"], task=task)
            self.store.add(db, {**row, "session": child},
                           (f"subagent-task:{child}", child, subtools.ACROSS,
                            {"text": text, "sender_session": sid}))

        try:
            if start_from == "fork":
                child = self.runtime.fork_finished(sid, title, source,
                                                   rows_beside)
            else:
                child = self.runtime.new_child(
                    sid, title, source, rows_beside, mode=mode, model=model,
                    parent_log=parent_log,
                )
        except Refused as error:
            return None, f"Not started: {error}."
        self.runtime.wake(child)
        return child, None

    def workflow_send(self, request):
        """subagent/send: a workflow's message for the session it works for
        (`for_session`): a new subagent of it, `text` its task (a handoff,
        no estimate: the workflow reads its answers), or `text` to one of
        its subagents (`session`), as a notice. The kernel has checked that
        a command sending it is that session's."""
        names = ("id", "text", "for_session", "source")
        for name in names:
            if not isinstance(request.get(name), str) or not request[name]:
                raise Refused(f"subagent/send needs {name}, a string")
        for name in ("session", "title", "mode", "model"):
            value = request.get(name)
            if value is not None and not isinstance(value, str):
                raise Refused(f"subagent/send's {name} must be a string")
        sid, text = request["for_session"], request["text"].strip()
        info = self.runtime.store.session(sid)
        if info is None:
            raise Refused(f"no session {sid!r}")
        if not text:
            raise Refused("subagent/send's text is empty")
        mode, model = request.get("mode"), request.get("model")
        if mode is not None:
            self.runtime.check_mode(mode)
            modes = self.runtime.modes
            if modes.index(mode) > modes.index(info["mode"]):
                raise Refused("a workflow can only choose a stricter mode")
        if model is not None:
            model = model.strip()
            if not model:
                raise Refused("a model needs a name")
        to = request.get("session")
        if to is not None:
            if mode is not None or model is not None:
                raise Refused("mode and model apply to a new subagent only")
            if to not in [row["session"] for row in self.below(sid)]:
                raise Refused(f"{to} isn't a subagent below session {sid}")
            payload = {"sender_session": sid, "text": subtools.WORKFLOW_MESSAGE.format(
                parent=sid, title=info["title"], text=text)}
            inbox = f"workflow:{request['id']}"
            self.store.notice(inbox, to, subtools.ACROSS, payload)
            self.runtime.wake(to)
            return {"ok": True, "session": to, "started": False, "inbox": inbox}
        deepest, depth = self.max_depth(), self.depth(sid) + 1
        if deepest <= 0:
            raise Refused("subagents are off (Subagents depth is 0 in ZERO's "
                          "settings)")
        if depth > deepest:
            levels = "level" if deepest == 1 else "levels"
            raise Refused(f"session {sid} is at level {depth - 1}, and "
                          f"subagents go {deepest} {levels} below a session "
                          "the user started")
        title = " ".join((request.get("title") or "").split()) or first_words(text)
        parent_log = self.runtime.read_log(sid)
        branch = self.runtime.read_branch(sid, log=parent_log)
        row = {
            "parent": sid, "start_from": "handoff", "depth": depth,
            "started_from_seq": branch[-1].seq if branch else None,
            "estimate_minutes": 0, "estimate_weighted_tokens": None,
            "estimate_dollars": 0, "started_through": WORKFLOW,
        }
        child, why = self.made(
            sid, title, "handoff", row, text, WORKFLOW,
            mode=mode, model=model, parent_log=parent_log,
        )
        if child is None:
            raise Refused(why)
        return {"ok": True, "session": child, "started": True,
                "inbox": f"subagent-task:{child}"}

    def send(self, sid, to, text, reply_to=None):
        """A message to any other session, by its id, as a notice. A
        subagent's message to its parent carries the record the parent
        started it from, so one reaching a part a rewind left behind is
        marked."""
        if to == sid:
            return "Error: that is this session."
        if self.runtime.store.session(to) is None:
            return f"Not sent: no session {to}."
        if reply_to is not None:
            child = self.store.subagent(to)
            ask = self.runtime.store.open_ask(to)
            if (child is None or child["parent"] != sid or ask is None
                    or ask.get("kind") != "question" or ask["id"] != reply_to):
                return "Not sent: no matching question from a direct helper."
            self.runtime.admit(Message(
                id=f"question-answer:{reply_to}", session=to,
                source=subtools.ACROSS, text=text, reply_to=reply_to,
            ), extra={"reply_kind": "question", "sender_session": sid})
            return f"{SENT} {to}."
        info = self.runtime.store.session(sid)
        payload = {"sender_session": sid, "text": subtools.MESSAGE.format(
            id=sid, title=info["title"], text=text)}
        row = self.store.subagent(sid)
        if row is not None and row["parent"] == to:
            payload["started_from_seq"] = row["started_from_seq"]
        self.store.notice(f"message:{uuid.uuid4()}", to, subtools.ACROSS, payload)
        self.runtime.wake(to)
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
        went through, and that holds a line the turn's messages there didn't
        (once it rests: with a message already queued, the next round's or a
        steer, the next turn's answer is the one), a turn it
        couldn't finish (a crash, a failure, a stop), both to its parent; a
        question waiting for the user, to the top session of its tree. Kept
        once by its id."""
        row = self.store.subagent(sid)
        if row is None or self.runtime.store.session(row["parent"]) is None:
            return
        # A workflow reads its subagent's answers and how its turns ended;
        # only a question waiting for the user goes up
        if by_workflow(row) and outcome != "waiting":
            return
        info = self.runtime.store.session(sid)
        found = {**row, "title": info["title"]}
        payload = {"sender_session": sid}
        if outcome == "waiting":
            pending = self.runtime.read_log(sid).unfinished_turn()
            parked = timeline.parked_ask(pending, current_turn=True)
            if parked is None:
                return
            ask = parked.entry
            key, to = ask.id, self.root(sid)
            if ask.get("kind") == "question":
                to = row["parent"]
                labels = entries.thaw(ask.get("choice_labels", {}))
                payload["text"] = (
                    f"Question {key} from helper {sid}: {ask.get('command')}\n"
                    f"Choices: {json.dumps(labels)}\n"
                    f"Its turn waits. {subtools.send_message.__name__} with "
                    f"session={sid} and reply_to={key} answers it."
                )
            else:
                payload["text"] = subtools.WAITING.format(
                    id=sid, title=info["title"], parent=row["parent"],
                    why=ask.get("why"))
        else:
            records = self.runtime.read_branch(sid)
            end = last_end(records)
            if end is None:
                return
            outcome = end.entry.get("outcome")
            answered = outcome == "answered"
            if answered and (
                self.runtime.store.next_queued(sid) is not None
                or sent_last(records, end, row["parent"])
                or answer_was_sent(records, end, row["parent"])
            ):
                return
            use = self.subtree_use(sid)
            key, to = end.seq, row["parent"]
            payload["text"] = (
                subtools.unsent_text(found, use, end.entry.get("answer")) if answered
                else subtools.report_text(found, outcome, use, end.entry.get("detail")))
            # The notice says how its use stands against its estimate
            if row["past_estimate_at"] is None and past(use, row):
                self.store.mark_past(sid, self.clock())
        if to == row["parent"]:
            payload["started_from_seq"] = row["started_from_seq"]
        self.store.notice(f"subagent:{sid}:{key}", to, subtools.UP, payload)
        self.runtime.wake(to)

    def check_estimates(self, sid):
        """The subagent's use, and each subagent's above it, against the
        estimate each was started with: past it, one notice to its parent."""
        while True:
            row = self.store.subagent(sid)
            if row is None or self.runtime.store.session(row["parent"]) is None:
                return
            if row["past_estimate_at"] is None and estimate_of(row):
                use = self.subtree_use(sid)
                if past(use, row):
                    info = self.runtime.store.session(sid)
                    text = subtools.PAST_ESTIMATE.format(
                        id=sid, title=info["title"],
                        use=describe(use),
                        passed=passed_text(
                            use, row["estimate_minutes"],
                            row["estimate_weighted_tokens"]),
                    )
                    self.store.notice(
                        f"subagent:{sid}:past_estimate", row["parent"],
                        subtools.UP,
                        {"text": text, "sender_session": sid,
                         "started_from_seq": row["started_from_seq"]},
                    )
                    self.store.mark_past(sid, self.clock())
                    self.runtime.wake(row["parent"])
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


def by_workflow(row):
    """Whether a subagent's row is one a workflow started."""
    return bool(row) and row.get("started_through") == WORKFLOW


def estimate_of(row):
    """A subagent's estimate, (minutes, weighted tokens), or None: a session
    the user started, or a subagent a workflow started, has none."""
    if not row or by_workflow(row):
        return None
    return row["estimate_minutes"], row["estimate_weighted_tokens"]


def first_words(text, chars=TITLE_CHARS):
    """A title from a task: its first line, cut after a word."""
    line = " ".join(text.strip().splitlines()[0].split()) if text.strip() else ""
    if len(line) <= chars:
        return line
    kept = line[:chars - 1].rsplit(" ", 1)[0] or line[:chars - 1]
    return kept.rstrip(" ,;:") + "…"


def last_end(records):
    """The session's own last turn_end, or None: a fork's copied turns are
    its parent's."""
    start = copied_to(records)
    return next((r for r in reversed(records)
                 if entries.has(r.entry, "turn_end") and r.seq > start), None)


def turn_calls(records, end):
    """The calls of the turn `end` closes, in order."""
    copied = copied_to(records)
    calls = []
    for record in reversed(records):
        if record.seq >= end.seq:
            continue
        if record.seq <= copied or entries.has(record.entry, "turn_end"):
            break
        if entries.has(record.entry, "tool_result"):
            calls.append(record)
    return calls[::-1]


def sent_text(call, parent):
    """What `call` sent `parent`, if it is a message to it that went through;
    else None."""
    entry = call.entry
    if (entry.get("tool") != "send_message"
            or not str(entry.get("result", "")).startswith(SENT)):
        return None
    try:
        args = json.loads(entry.get("arguments") or "{}")
    except ValueError:
        return None
    if not isinstance(args, dict):
        return None
    if str(args.get("session", "")).strip() != parent:
        return None
    return str(args.get("text", ""))


def sent_last(records, end, parent):
    """Whether the turn `end` closes made, as its last call, a message to
    `parent` that went through (DSH's rule: any call after it voids it)."""
    calls = turn_calls(records, end)
    return bool(calls) and sent_text(calls[-1], parent) is not None


def answer_was_sent(records, end, parent):
    """Whether every line of the answer `end` holds, its marks aside, is in a
    message the same turn sent `parent` that went through: a report sent,
    a call after it, then the report again as the answer."""
    sent = [text for call in turn_calls(records, end)
            if (text := sent_text(call, parent)) is not None]
    if not sent:
        return False
    said = " ".join(plain(text) for text in sent)
    lines = [plain(line) for line in (end.entry.get("answer") or "").splitlines()]
    return all(line in said for line in lines if line)


def plain(text):
    """`text` without Markdown's marks (code, emphasis, headings, quotes)
    and with its spaces collapsed, so a line reworded only in marks matches."""
    return " ".join(MARKS.sub("", text).split())


def copied_to(records):
    """The last record a fork copied from its parent, else 0."""
    head = records[0].entry if records else {}
    return (head.get("forked_from") or {}).get("seq", 0)


def past(use, row):
    """Whether a use passes a subagent's estimate."""
    return past_estimate(use, row["estimate_minutes"],
                         row["estimate_weighted_tokens"])

