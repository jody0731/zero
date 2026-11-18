"""Question parking is independent of every permission mode."""

from core.hooks import Deny, Hook, Question
from questions.approval import check_approval, proposal_for
from questions.tool import NAME


class Questions(Hook):
    def before_tool(self, call):
        return check_approval(call)

    def approve(self, call):
        if call.name != NAME:
            return None
        labels = {str(index): text for index, text in
                  enumerate(call.args.get("options", []), 1)}
        text = call.args["question"]
        if "proposed_call" in call.args:
            proposed = call.args["proposed_call"]
            session = call.turn.session
            try:
                proposal, shown = proposal_for(
                    proposed["tool"], proposed["arguments"], session.tool_context)
            except (ValueError, OSError) as error:
                return Deny(f"No proposal opened: {error}")
            call.notes["approval_proposal"] = proposal
            call.notes["approval_session"] = str(session.log.path)
            text += "\n\n" + shown
            labels = {"yes": "Approve this change", "no": "Do not change"}
        call.notes["question"] = {
            "text": text, "choice_labels": labels,
        }
        return Question(text, labels)
