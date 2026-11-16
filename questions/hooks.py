"""Question parking is independent of every permission mode."""

from core.hooks import Hook, Question
from questions.tool import NAME


class Questions(Hook):
    def approve(self, call):
        if call.name != NAME:
            return None
        labels = {str(index): text for index, text in
                  enumerate(call.args.get("options", []), 1)}
        call.notes["question"] = {
            "text": call.args["question"], "choice_labels": labels,
        }
        return Question(call.args["question"], labels)
