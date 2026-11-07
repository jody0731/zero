"""Limits: why a turn stops, asks to go on, or has a call refused.

steps.py: the steps a turn takes before it asks to go on (a soft cap). stops.py:
what ends a turn with its reason (agent time, a full context, malformed
replies). repeats.py: a call made again soon isn't run.
"""

from settings import Setting

SETTINGS = (
    Setting("max_steps", "Limits", "Steps before a turn asks to go on", 50, "int"),
    Setting("extra_steps", "Limits", "Steps a yes to go on adds", 50, "int"),
    Setting("max_seconds", "Limits", "Seconds a turn may take in all", None,
            "number", unset_means="no limit"),
    Setting("context_stop", "Limits", "Tokens of context that stop a turn", None,
            "int", minimum=1000, unset_means="the model's own"),
    Setting("max_broken_replies", "Limits", "Malformed replies that stop a turn",
            3, "int"),
)
