"""Limits: why a turn stops, asks to go on, or has a call refused.

steps.py: the steps a turn takes before it asks to go on (a soft cap). stops.py:
what ends a turn with its reason (agent time, a full context, malformed
replies). repeats.py: a call made again soon isn't run.
"""

from settings import Setting

SETTINGS = (
    Setting("max_steps", "Limits", "Steps before a turn asks to go on", 50, "int"),
    Setting("step_grant", "Limits", "Steps a yes to go on adds", 50, "int"),
    Setting("max_seconds", "Limits", "Seconds a turn may take in all", None,
            "number", off="no limit"),
    Setting("context_stop", "Limits", "Tokens of context that stop a turn", None,
            "int", least=1000, off="the model's own"),
    Setting("max_format_errors", "Limits", "Malformed replies that stop a turn",
            3, "int"),
)
