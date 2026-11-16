"""User consent before default hands work to its unattended approver."""

from core import history, tools
from core.hooks import Hook, ModelAsk, Stop


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
        if record["type"] == "ask" and record.get("notes", {}).get("handoff"):
            asked.add(record["ask"])
        elif (record["type"] == "answer" and record.get("yes")
              and record.get("choice") == "session"
              and record["ask"] in asked):
            allowed.add(record["ask"])
        elif record["type"] == "forget":
            forgotten.add(record.get("grant"))
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
        entry = tools.entry_of(call.name)
        declared = entry and entry.get("hands_off")
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
        pending = history.unfinished_turn(session.log.branch())
        asks = {r["ask"] for r in pending if r["type"] == "ask"
                and r.get("notes", {}).get("before_model")}
        answers = [r for r in pending if r["type"] == "answer"
                   and r["ask"] in asks]
        if answers and not answers[-1].get("yes"):
            return Stop("declined", "the workflow hand-off was not approved")
        if answers or any(r["type"] == "assistant" for r in pending):
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
