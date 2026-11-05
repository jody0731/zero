"""The judge: a model that checks each command serves the user's goal, the
approver of the auto modes. It plugs in as an approver and knows nothing of
the permissions; its "session" verdicts reach the grants as log records.

verdict.py: the judge itself, one model call per verdict. plugin.py: what it
sees of a session, and the approver (AutoApproval).
"""

from model import EFFORTS
from settings import Setting

SETTINGS = (
    Setting("judge_model", "New sessions", "The judge's model", None, "model",
            off="the session's model"),
    # The judge's own; unset sends none, so the API's default holds
    Setting("judge_effort", "New sessions", "The judge's thinking (DeepSeek)",
            None, "choice", choices=EFFORTS, off="the API's default"),
    Setting("judge_timeout", "Limits", "Seconds the judge may stay silent", 60,
            "number"),
    # A reply that isn't a verdict is asked again; still none, a deny
    Setting("judge_retries", "Limits", "Retries after the judge gives no verdict",
            1, "int", least=0),
    # What the judge sees of a command's output, cut in the middle, and
    # how many of the latest commands show it, so a long session stays small
    Setting("judge_output_chars", "Limits",
            "Characters of a command's output the judge sees", 1000, "int",
            least=100),
    Setting("judge_outputs", "Limits",
            "Latest commands whose output the judge sees", 10, "int", least=0),
)
