"""Private values enter through the app protocol, never through the inbox."""

import copy
import json
import threading

from core import history
from server.runtime import Message
from server.services import Refused, Service
from vault.boundary import Boundary
from vault.hooks import VaultInput
from vault.search import find
from vault.store import Vault, VaultError


class VaultService(Service):
    name = "vault"

    def __init__(self, make_vault=Vault):
        self.make_vault = make_vault
        self.input_lock = threading.Lock()

    def attach(self, runtime):
        super().attach(runtime)
        self.vault = self.make_vault(runtime.paths.home)
        self.boundary = Boundary(self.vault)

    def redact(self, value, partial=False):
        return self.boundary.redact(value, partial=partial)

    def messages(self):
        return {"vault/answer": self.answer, "vault/status": self.status}

    def status(self, request):
        try:
            return {"ok": True, "status": self.vault.status()}
        except VaultError as error:
            raise Refused(str(error)) from None

    def session_built(self, sid, session):
        session.tool_context.update({
            "vault": self.vault, "tool_boundary": self.boundary,
            "vault_find": lambda query, notes: self.find(session, query, notes),
        })
        session.hooks.hooks.insert(0, VaultInput(self.vault))

    def find(self, session, query, notes):
        model = copy.copy(session.model)
        model.effort = "off"
        return find(self.redact(self.vault.labels()), self.redact(query),
                    model, notes)

    def answer(self, request):
        with self.input_lock:
            return self.save_answer(request)

    def save_answer(self, request):
        sid, ask_id = request.get("session"), request.get("ask")
        if not isinstance(sid, str) or not isinstance(ask_id, str):
            raise Refused("A vault answer needs its session and ask id.")
        message_id = "vault-answer-" + ask_id
        prior = self.runtime.store.query(
            "SELECT payload FROM inbox WHERE id = ? AND session = ?",
            message_id, sid)
        if prior:
            return {"ok": True, "saved": bool(
                json.loads(prior[0]["payload"]).get("private_saved"))}
        state = self.runtime.state(sid)
        with state.lock:
            ask = history.parked_ask(self.runtime.log(sid).branch())
            if not ask or ask["ask"] != ask_id or ask.get("kind") != "secret":
                raise Refused("That private input no longer waits.")
            spec = ask["notes"]["private_input"]
            cancelled = (request.get("cancel") is True
                         and not self.vault.saved(ask_id))
            if not cancelled:
                try:
                    self.vault.save(spec["action"], spec["labels"],
                                    request.get("values"), ask_id)
                except VaultError as error:
                    raise Refused(str(error)) from None
        # Only this receipt enters the durable inbox. The encrypted file keeps
        # the ask receipt too, so a retry after a crash cannot overwrite values.
        status = ("Private input cancelled." if cancelled
                  else "Private information saved; ready to use for the task.")
        self.runtime.admit(Message(
            id=message_id, text=status, source="user",
            session=sid, reply_to=ask_id,
        ), extra={"reply_kind": "secret", "private_saved": not cancelled})
        return {"ok": True, "saved": not cancelled}

    def reply(self, sid, ask, payload):
        if ask.get("kind") == "secret":
            return ("yes" if payload.get("private_saved") else "no"), None
