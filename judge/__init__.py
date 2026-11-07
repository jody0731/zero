"""The judge: a model that checks each command serves the user's goal, the
approver of the auto modes. It plugs in as an approver and knows nothing of
the permissions; its "session" verdicts reach the grants as log records.

Its settings are a design choice, not a user's knob: the settings view keeps
them in Dev. verdict.py: the judge itself, one model call per verdict.
plugin.py: what it sees of a session, and the approver (AutoApproval).
"""

from model import EFFORT_NEEDS, EFFORTS
from settings import Setting

SETTINGS = (
    # Unset, the judge is the session's model, as it stands
    Setting("judge_model", "Judge", "Model", None, "model",
            off="the session's model"),
    # Off: the judge reasons in its answer, before its decision (verdict.py),
    # which is faster and doesn't circle as thinking can at temperature 0
    Setting("judge_effort", "Judge", "Thinking effort", "off", "choice",
            choices=EFFORTS, needs=EFFORT_NEEDS, runs_on="judge_model"),
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
