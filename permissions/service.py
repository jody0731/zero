"""The permissions' service in the server (server/services.py): the reply step, and the
`grant/list` and `grant/forget` messages.

The reply step runs when a reply reaches a parked ask. One of the ask's
choices picked at the box is the answer; words are read by the consent check
(consent.py), and a plain yes runs the call, with the grant the words ask for
if the ask offers it. A "saved" answer writes its grant to the user's file
(grants.py).
"""

from server.services import Service, Refused
from server.runtime import picked
from permissions import grants
from permissions.consent import chosen_answer, question
from permissions.modes import JUDGED

# Why a session's grant can't be taken back now
TURN_HOLDS = (
    "a turn is running or waiting for an answer in this session; take it back "
    "once it has ended"
)


class PermissionsService(Service):
    name = "permissions"
    looking_messages = ("grant/list",)

    def __init__(self, make_consent, saved_permissions=None):
        """`make_consent(model)` builds the consent check for a session's
        model (the assembly's, which knows the models). `saved_permissions()`:
        the user's file of saved permissions, where a "saved" answer goes;
        None for none (the evals), when it counts for the session."""
        self.make_consent = make_consent
        self.saved_permissions = saved_permissions
        # One consent check per model: model name → Consent
        self.consents = {}

    def messages(self):
        return {"grant/list": self.list_message, "grant/forget": self.forget_message}

    def list_message(self, request):
        sid = request.get("session")
        if not isinstance(sid, str):
            raise Refused("grant/list needs a session string")
        return {"ok": True, "grants": self.grants(sid)}

    def forget_message(self, request):
        sid, grant = request.get("session"), request.get("grant")
        if not isinstance(sid, str):
            raise Refused("grant/forget needs a session string")
        if not isinstance(grant, str):
            raise Refused("grant/forget needs the grant's id, a string")
        return {"ok": True, **self.forget_grant(sid, grant)}

    def reply(self, sid, ask, payload):
        choice, verdict = picked(ask, payload), None
        if payload.get("reply_to") == ask["ask"] and choice is None:
            verdict = self.consent(sid, ask, payload["text"])
            choice = chosen_answer(verdict, ask)
        path = self.saved_permissions and self.saved_permissions()
        grants.save_grant(path, ask, choice, sid)
        return choice, verdict

    def consent(self, sid, ask, text):
        model = self.runtime.store.session(sid)["model"]
        if model not in self.consents:
            self.consents[model] = self.make_consent(model)
        return self.consents[model].decide(question(ask), text)

    def grants(self, sid):
        """The grants that hold in a session: its own, the judge's where
        the judge answers, and the user's saved ones; each with its words."""
        info = self.runtime.store.session(sid)
        if info is None:
            raise Refused(f"no session {sid!r}")
        path = self.saved_permissions and self.saved_permissions()
        records = list(self.runtime.log(sid).branch())
        for service in self.runtime.services.values():
            records.extend(service.permission_records(sid))
        other_session_asks = {
            r["ask"] for r in records if r["type"] == "ask"
            and r.get("notes", {}).get("handoff_scope") not in (None, sid)
        }
        records = [r for r in records if r.get("ask") not in other_session_asks]
        records.sort(key=lambda record: record["time"])
        held = grants.listing(records, path, info["mode"] in JUDGED)
        return [{**g, "description": grants.describe(g["grant"])} for g in held]

    def forget_grant(self, sid, grant_id):
        """Take a grant back: a saved one from the user's file, a session's
        with a `forget` record (refused while a turn runs or waits)."""
        found = {g["id"]: g for g in self.grants(sid)}.get(grant_id)
        if found is None:
            raise Refused(f"no permission {grant_id!r} in this session")
        if found["scope"] == "saved":
            grants.forget_saved(self.saved_permissions(), grant_id)
            return {"forgot": grant_id}
        self.runtime.append_between_turns(
            sid, TURN_HOLDS, "forget", grant=grant_id
        )
        return {"forgot": grant_id}
