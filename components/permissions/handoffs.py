"""User consent before default hands work to its unattended approver."""

from core import entries, tools
from core.hooks import Hook, ModelAsk, Stop
from components.permissions.grants import is_revocation


ABOUT = (
    "ZERO is handing work off. After approval, the judge answers its "
    "permission asks while that work runs."
)
OFFER = "handing work off"


def session_permission(records):
    """A user's session consent, read from the log, never a judge verdict."""
    asked = set()
    allowed = set()
    forgotten = set()
    for record in records:
        entry = record.entry
        if entries.has(entry, "ask") and (entry.get("notes") or {}).get("handoff"):
            asked.add(entry.id)
        elif (entries.has(entry, "answer") and entry.get("yes")
              and entry.get("choice") == "session"):
            if (ask := entries.linked_to(entry, "answers")) in asked:
                allowed.add(ask)
        elif is_revocation(entry):
            forgotten.add(entry.get("grant"))
    return bool(allowed - forgotten)


class HandoffApproval(Hook):
    """Ordinary grants cannot stand in for consent to unattended work."""

    def __init__(self, unattended, enabled=True):
        self.unattended = unattended
        self.enabled = enabled
        self.admitted_turn = None

    def before_tool(self, call):
        if not self.enabled:
            return None
        meta = tools.metadata_of(call.name, "permissions") or {}
        declared = meta.get("hands_off")
        if not declared or (callable(declared) and not declared(call.args)):
            return None
        if self.unattended(call.turn):
            return None
        if self._permitted(call.turn.session):
            return None
        call.notes["handoff"] = {
            "action": "ask", "reason": "handing work off", "about": ABOUT,
        }
        call.notes["offer_text"] = OFFER
        return None

    def _permitted(self, session):
        records = session.tool_context.get("handoff_records")
        return session_permission(records() if records else session.log.branch())

    def before_model(self, step):
        session = step.turn.session
        admission = session.tool_context.get("workflow_handoff")
        if admission is None or self.admitted_turn is step.turn:
            return None
        pending = session.log.unfinished_turn()
        asks = {r.entry.id for r in pending if entries.has(r.entry, "ask")
                and (r.entry.get("notes") or {}).get("before_model")}
        answers = [r.entry for r in pending if entries.has(r.entry, "answer")
                   and entries.linked_to(r.entry, "answers") in asks]
        if answers and not answers[-1].get("yes"):
            return Stop("declined", "the workflow hand-off was not approved")
        if answers or any(entries.has(r.entry, "assistant_reply") for r in pending):
            self.admitted_turn = step.turn
            return None
        scope = admission(step.turn)
        if scope is None or self._permitted(session):
            self.admitted_turn = step.turn
            return None
        return ModelAsk(
            "handing work off", step.turn.question,
            {"handoff": {"action": "ask", "reason": "handing work off"},
             "handoff_scope": scope, "session_scope": scope, "offer_text": OFFER},
            about=ABOUT,
        )
