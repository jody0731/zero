"""Inbox admission and routing to a session or its open ask."""

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from core import attribution, entries, timeline
from core.asking import NO, YES
from server import replies
from server.idempotency import (
    legacy_request_hash,
    matches_request_hash,
    request_hash,
)
from server.services import Refused
from server.session_actions import ID_TRIES, check_switchable, new_id, title
from server.store import ReplyUnavailable, UnknownSession

WHEN_BUSY = ("steer", "queue")
# A harness note marks the request to continue.
CONTINUE = "[The user asked you to go on with the task.]"


@dataclass
class Message:
    """A message from a client. `session` None starts a new session."""

    id: str
    text: str
    session: str | None = None
    mode: str | None = None
    model: str | None = None
    # A new session's thinking effort; None: config.toml's
    thinking_effort: str | None = None
    # Who sent it: each caller names itself (the CLI, terminal-<pid>)
    source: str = field(kw_only=True)
    # The ask this message replies to: the client marks a line typed at an
    # ask's prompt
    reply_to: str | None = None
    # One of the ask's choices, picked at the box: no consent check
    choice: str | None = None
    # A new session's workspace, an absolute folder; None means /
    workspace: str | None = None
    # While a turn runs: "steer" it, or "queue" as a follow-up after its answer
    when_busy: str = "steer"
    title: str | None = None


def sent_before(
    row, message, digest, *, legacy_digest=None, allow_legacy=False,
):
    """What a message sent again gets, its id already in the inbox: the
    first's ack, nothing run again; refused when the id came with another
    message (its text, or the session it named)."""
    payload = json.loads(row["payload"])
    same = (row["kind"] == "user" and payload.get("text") == message.text
            and row["source"] == message.source
            and payload.get("reply_to") == message.reply_to
            and payload.get("choice") == message.choice
            and matches_request_hash(
                payload.get("request_hash", digest),
                digest,
                legacy_digest=legacy_digest,
                allow_legacy=allow_legacy,
            )
            and (message.session is None or message.session == row["session"]))
    if not same:
        raise Refused(f"message id {message.id!r} was used for another "
                      "message; a new message takes a new id")
    ack = {"admitted": False, "session": row["session"]}
    if payload.get("handled_as") is not None:
        ack["handled_as"] = payload["handled_as"]
    return ack


def picked(ask, payload):
    """The choice picked at an ask's box, if the reply is marked for that
    ask and the ask offers it; else None."""
    choice = payload.get("choice")
    entry = ask.entry
    offered = entry.get("choices") or [YES, NO]
    if payload.get("reply_to") != entry.id or choice not in offered:
        return None
    return choice


class Admission:
    def __init__(self, runtime):
        self.runtime = runtime

    def admit(self, message, extra=None, *, legacy_send=False, principal=None):
        """Commit a message to the inbox, then return the ack: its session and
        its handled_as, what became of it. `extra`: payload fields."""
        runtime = self.runtime
        if not runtime.accepting:
            raise Refused("the server is shutting down; send it again once it's back")
        if message.when_busy not in WHEN_BUSY:
            raise Refused(f"when_busy is one of {WHEN_BUSY}")
        req_payload = {**vars(message), "fields": extra or {}}
        digest = request_hash(req_payload, principal=principal)
        seen = runtime.store.query("SELECT * FROM inbox WHERE id = ?", message.id)
        if seen:
            return self._sent_before(
                seen[0], message, digest, req_payload, principal,
            )
        prepared = runtime.prepare_message(message, extra, principal=principal)
        has_content = any(item.has_content for item in prepared)
        if not message.text.strip() and not has_content:
            raise Refused("empty message")
        fallback_title = next(
            (item.fallback_title for item in prepared if item.fallback_title),
            None,
        )
        merged_extra = dict(extra or {})
        for item in prepared:
            if item.payload:
                merged_extra.update(item.payload)
        claims = tuple(item.claim for item in prepared if item.claim is not None)
        after_commits = tuple(
            item.after_commit for item in prepared if item.after_commit is not None
        )
        new = None
        sid = message.session
        if message.title is not None and (sid is not None or not message.title.strip()):
            raise Refused("an initial title needs a new session and a nonblank name")
        if sid is None:
            workspace = message.workspace or "/"
            if not Path(workspace).is_absolute():
                raise Refused(f"a workspace is an absolute folder, not {workspace!r}")
            if not Path(workspace).is_dir():
                raise Refused(f"no folder {workspace}")
            mode = runtime.check_mode(message.mode or runtime.mode)
            model = message.model or runtime.model
            if not model:
                raise Refused(
                    "no model is set yet: zero, the terminal, connects one "
                    "(Connect a model; later /settings, Models and keys), or "
                    "--model names one"
                )
            initial_title = message.title or message.text
            if not initial_title.strip() and fallback_title:
                initial_title = fallback_title
            new = {
                "title": title(initial_title) or "Attachment",
                "source": message.source,
                "model": model,
                "mode": mode,
                "workspace": str(Path(workspace).resolve()),
            }
            initial_switch = {}
            if message.thinking_effort is not None:
                # Its first turn builds it with the effort, as a switch would
                initial_switch["thinking_effort"] = check_switchable(
                    "thinking_effort", message.thinking_effort,
                )
            for sw_name, sw_value in (
                merged_extra.pop("initial_switches", None) or {}
            ).items():
                initial_switch[sw_name] = check_switchable(sw_name, sw_value)
            if initial_switch:
                new["switch"] = json.dumps(initial_switch)
        else:
            merged_extra.pop("initial_switches", None)
        payload = {"text": message.text, "request_hash": digest, **merged_extra}
        if message.choice is not None and message.reply_to is None:
            raise Refused("a choice answers the question named in reply_to")
        if message.reply_to is not None and sid is None:
            raise Refused("a reply needs the session of its question")
        if message.reply_to is not None:
            payload["reply_to"] = message.reply_to
            if message.choice is not None:
                payload["choice"] = message.choice
        with runtime.door:
            if not runtime.accepting:
                raise Refused("the server is shutting down; send it again once "
                              "it's back")
            runtime.admitting += 1
            runtime.admissions += 1
        try:
            admitted, sid, handled_as = runtime.admit_to_session(
                message, sid, new, payload, legacy_send, claims=claims,
                legacy_payload=req_payload, principal=principal,
            )
            if admitted:
                for after_commit in after_commits:
                    after_commit(sid)
                if runtime.started:
                    runtime.dispatch(sid)
        finally:
            with runtime.activity:
                runtime.admitting -= 1
                runtime.activity.notify_all()
        return {"admitted": admitted, "session": sid, "handled_as": handled_as}

    def check_reply(self, sid, message, expected):
        runtime = self.runtime
        ask = runtime.store.open_ask(sid)
        if not ask or ask["id"] != message.reply_to:
            raise Refused("the addressed question no longer waits in this session")
        private = runtime.private_ask_kinds
        if ask["kind"] in private and expected != ask["kind"]:
            raise Refused(private[ask["kind"]])
        if expected and ask["kind"] != expected:
            raise Refused("the addressed question has another kind")
        if ask["requires_user"] and not attribution.is_user_source(message.source):
            raise Refused("this change needs an answer from the user")
        if message.choice is not None:
            offered = replies.ask_row(ask)["choices"]
            if message.choice not in offered:
                raise Refused(f"the question's choices are {', '.join(offered)}, "
                              f"not {message.choice!r}")

    def _legacy_inbox_allowed(self, message, principal):
        if principal is None:
            return True
        if principal.is_command or principal.command_session is not None:
            return False
        if principal.scope is not None:
            return (
                message.session is not None
                and self.runtime.in_workflow_scope(message.session, principal.scope)
            )
        return True

    def _sent_before(self, row, message, digest, req_payload, principal):
        legacy_digest = (
            legacy_request_hash(req_payload)
            if principal is not None and req_payload is not None else None
        )
        return sent_before(
            row,
            message,
            digest,
            legacy_digest=legacy_digest,
            allow_legacy=self._legacy_inbox_allowed(message, principal),
        )

    def admit_to_session(self, message, sid, new, payload, legacy_send,
                         claims=(), *, legacy_payload=None, principal=None):
        """(admitted, session id, handled_as): what becomes of the message
        decided and its row committed under the session's own lock. A new
        session takes a new id, and another when one is taken."""
        runtime = self.runtime
        for tries in range(1, ID_TRIES + 1):
            if new:
                sid = new_id()
            state = runtime.state(sid)
            with state.lock:
                if message.reply_to is not None:
                    seen = runtime.store.query("SELECT * FROM inbox WHERE id = ?",
                                            message.id)
                    if seen:
                        ack = self._sent_before(
                            seen[0],
                            message,
                            payload["request_hash"],
                            legacy_payload,
                            principal,
                        )
                        return False, ack["session"], ack["handled_as"]
                    runtime.check_reply(sid, message, payload.get("reply_kind"))
                    handled_as = "reply"
                else:
                    handled_as = ("started" if new else
                                  runtime.handled_as(sid, state, message, legacy_send))
                said = dict(payload)
                if handled_as == "queued":
                    said["follow_up"] = True
                # What a copy of it sent again is told (sent_before)
                said["handled_as"] = handled_as
                try:
                    admitted, sid = runtime.store.admit(
                        message.id, sid, "user", message.source, said,
                        new_session=new, claims=claims,
                    )
                except ReplyUnavailable as error:
                    raise Refused(str(error)) from None
                except UnknownSession:
                    raise Refused(f"no session {sid!r}") from None
                except sqlite3.IntegrityError:
                    if not new or tries == ID_TRIES:
                        raise
                    continue
                if not admitted:
                    row, = runtime.store.query("SELECT * FROM inbox WHERE id = ?",
                                            message.id)
                    ack = self._sent_before(
                        row,
                        message,
                        payload["request_hash"],
                        legacy_payload,
                        principal,
                    )
                    handled_as = ack["handled_as"]
                return admitted, sid, handled_as

    def handled_as(self, sid, state, message, legacy_send=False):
        """What becomes of a message to this session, decided under its
        lock: `reply` to its open ask, `steered` or `queued` (a follow-up)
        while a turn runs or waits to, `started` otherwise."""
        runtime = self.runtime
        info = runtime.store.session(sid)
        if info is None:
            raise Refused(f"no session {sid!r}")
        if info["state"] == "waiting":
            if legacy_send:
                raise Refused("A question is waiting. Name it with reply_to "
                              "to answer, or use session/submit for new work.")
            return "queued"
        busy = state.runner is not None or runtime.store.has_queued(sid)
        if not busy:
            return "started"
        return "queued" if message.when_busy == "queue" else "steered"

    def continue_turn(self, sid, msg_id, source):
        """Take on a turn that ended without an answer (stopped, failed, past a
        limit), with no new message: the turn opens with a harness note. Refused
        while a turn runs, resumes or waits on an ask (Pi's `continue`)."""
        runtime = self.runtime
        info = runtime.store.session(sid)
        if info is None:
            raise Refused(f"no session {sid!r}")
        seen = runtime.store.query("SELECT * FROM inbox WHERE id = ?", msg_id)
        if seen:
            message = Message(msg_id, CONTINUE, session=sid, source=source)
            digest = request_hash({**vars(message), "fields": {"continue": True}})
            return sent_before(seen[0], message, digest)
        state = runtime.state(sid)
        with state.lock:
            if state.runner is not None or runtime.store.has_queued(sid):
                raise Refused("a turn is running in this session; nothing to continue")
            if runtime.store.open_ask(sid) is not None:
                raise Refused("a question is waiting in this session: answer it")
        end = next((r for r in reversed(runtime.read_branch(sid))
                    if entries.has(r.entry, "turn_end")), None)
        if end is None:
            raise Refused("this session has no turn to go on with")
        if end.entry.get("outcome") == "answered":
            raise Refused("its last turn was answered; send a message instead")
        return runtime.admit(
            Message(msg_id, CONTINUE, session=sid, source=source),
            extra={"continue": True},
        )

    def stop_turn(self, sid, msg_id, source):
        """Commit the stop before setting its flag or sending the ack."""
        runtime = self.runtime
        state = runtime.state(sid)
        with state.lock:
            if not runtime.accepting:
                raise Refused("the server is shutting down")
            seen = runtime.store.query("SELECT * FROM inbox WHERE id = ?", msg_id)
            if seen:
                if seen[0]["kind"] != "stop" or seen[0]["session"] != sid:
                    raise Refused("message id already used")
                return {"stopped": True, "withdrawn": []}
            running = state.running
            if running is None:
                return {"stopped": False, "withdrawn": []}
            log = runtime.read_log(sid)
            pending = log.unfinished_turn()
            if not pending or timeline.parked_ask(pending, current_turn=True):
                return {"stopped": False, "withdrawn": []}
            control, row = running
            try:
                withdrawn = runtime.store.admit_stop(msg_id, sid, source, row)
            except ValueError as error:
                raise Refused(str(error)) from None
            control.stop_detail = f"stop from {source}"
            control.stop.set()
        if withdrawn:
            runtime.publish(sid, {"withdrawn": withdrawn})
        return {"stopped": True, "withdrawn": withdrawn}
