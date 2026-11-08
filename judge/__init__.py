"""The judge: a model that checks each command serves the user's goal, the
approver of the auto modes. It plugs in as an approver and knows nothing of
the permissions; its "session" verdicts reach the grants as log records.

Its settings are a design choice, not a user's knob: the settings view keeps
them in Dev. verdict.py: the judge itself, one model call per verdict.
hook.py: what it sees of a session, and the approver (JudgeApproval).
"""

from model import THINKING_EFFORT_NEEDS, THINKING_EFFORTS
from settings import Setting

SETTINGS = (
    # Unset, the judge is the session's model, as it stands
    Setting("judge_model", "Judge", "Model", None, "model",
            unset_means="the session's model"),
    # Off: the judge reasons in its answer, before its decision (verdict.py),
    # which is faster and doesn't circle as thinking can at temperature 0
    Setting("judge_thinking_effort", "Judge", "Thinking effort", "off", "choice",
            choices=THINKING_EFFORTS, needs=THINKING_EFFORT_NEEDS,
            runs_on="judge_model"),
    # As the model's (model/__init__.py): a backstop, the judge's view now as
    # long as half its model's window
    Setting("judge_silence_seconds", "Limits", "Seconds the judge may stay silent", 300,
            "number"),
    # The ceiling on a verdict's reply, its thinking included: silence alone
    # never stops a judge that keeps writing (Qwen3.6 wrote 41,840 tokens in
    # 20 minutes; Ornith's thinking circled to 16,384 twice), and real
    # verdicts take 77-124 tokens, 1,047 at most (8 Nov). Tokens, not
    # seconds: a long view's reading on a slow server counts for nothing.
    # Past it, no verdict: asked again, then a deny
    Setting("judge_reply_tokens", "Limits", "Tokens a verdict may write", 8192,
            "int", minimum=1024),
    # A reply that isn't a verdict is asked again; still none, a deny
    Setting("judge_retries", "Limits", "Retries after the judge gives no verdict",
            1, "int", minimum=0),
    # What the judge sees of each command's output, cut in the middle; every
    # command's stays, so each call's view starts with the last one's and the
    # server's cache serves it (6.3)
    Setting("judge_output_chars", "Limits",
            "Characters of a command's output the judge sees", 1000, "int",
            minimum=100),
)
