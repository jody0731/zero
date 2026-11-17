"""A private input parks independently of permission modes and the judge."""

from core.hooks import Deny, Hook, Question
from vault.store import VaultError
from vault.tool import NAME


class VaultInput(Hook):
    def __init__(self, vault):
        self.vault = vault

    def approve(self, call):
        if call.name != NAME or call.args["action"] not in ("add", "update"):
            return None
        labels = call.args["labels"]
        try:
            with self.vault.lock:
                self.vault.check(call.args["action"], labels)
        except VaultError as error:
            return Deny(str(error))
        call.notes["private_input"] = {
            "action": call.args["action"], "labels": labels,
        }
        form = call.args["input_form"]
        fields = {field["part"]: field["label"] + "\n" + field["instructions"]
                  for field in form["fields"]}
        question = Question(form["instructions"], fields, kind="secret")
        question.about = form["title"]
        return question
