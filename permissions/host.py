"""The permissions' part in the host (host/parts.py): the reply step, and the
`permissions` and `forget` verbs.

The reply step runs when a reply reaches a parked ask. One of the ask's
choices picked at the box is the answer; words are read by the consent check
(consent.py), and a plain yes runs the call, with the grant the words ask for
if the ask offers it. A "saved" answer writes its grant to the user's file
(grants.py).
"""

from host.parts import Part, Refused
from host.runtime import picked
from permissions import grants
from permissions.consent import chosen, question
from permissions.modes import JUDGED

# Why a session's grant can't be taken back now
TURN_HOLDS = (
    "a turn is running or waiting for an answer in this session; take it back "
    "once it has ended"
)


class PermissionsHost(Part):
    name = "permissions"
    looking = ("permissions",)

    def __init__(self, make_consent, saved_permissions=None):
        """`make_consent(model)` builds the consent check for a session's
        model (the assembly's, which knows the models). `saved_permissions()`:
        the user's file of saved permissions, where a "saved" answer goes;
        None for none (the evals), when it counts for the session."""
        self.make_consent = make_consent
        self.saved_permissions = saved_permissions
        # One consent check per model: model name → Consent
        self.consents = {}

    def verbs(self):
        return {"permissions": self.permissions_verb, "forget": self.forget_verb}

    def permissions_verb(self, request):
        sid = request.get("session")
        if not isinstance(sid, str):
            raise Refused("permissions needs a session string")
        return {"ok": True, "grants": self.permissions(sid)}

    def forget_verb(self, request):
        sid, grant = request.get("session"), request.get("grant")
        if not isinstance(sid, str):
            raise Refused("forget needs a session string")
        if not isinstance(grant, str):
            raise Refused("forget needs the grant's id, a string")
        return {"ok": True, **self.forget(sid, grant)}

    def reply(self, sid, ask, payload):
        choice, verdict = picked(ask, payload), None
        if payload.get("reply_to") == ask["ask"] and choice is None:
            verdict = self.consent(sid, ask, payload["text"])
            choice = chosen(verdict, ask)
        path = self.saved_permissions and self.saved_permissions()
        grants.keep(path, ask, choice, sid)
        return choice, verdict

    def consent(self, sid, ask, text):
        model = self.runtime.store.session(sid)["model"]
        if model not in self.consents:
            self.consents[model] = self.make_consent(model)
        return self.consents[model].decide(question(ask), text)

    def permissions(self, sid):
        """The grants that hold in a session: its own, the judge's where
        the judge answers, and the user's saved ones; each with its words."""
        info = self.runtime.store.session(sid)
        if info is None:
            raise Refused(f"no session {sid!r}")
        path = self.saved_permissions and self.saved_permissions()
        held = grants.listing(
            self.runtime.log(sid).records(), path, info["mode"] in JUDGED
        )
        return [{**g, "what": grants.describe(g["grant"])} for g in held]

    def forget(self, sid, grant_id):
        """Take a grant back: a saved one from the user's file, a session's
        with a `forget` record (refused while a turn runs or waits)."""
        found = {g["id"]: g for g in self.permissions(sid)}.get(grant_id)
        if found is None:
            raise Refused(f"no permission {grant_id!r} in this session")
        if found["scope"] == "saved":
            grants.forget_saved(self.saved_permissions(), grant_id)
            return {"forgot": grant_id}
        self.runtime.append_between_turns(
            sid, TURN_HOLDS, "forget", grant=grant_id
        )
        return {"forgot": grant_id}
