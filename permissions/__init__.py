"""The permissions: who answers when a call asks, and what was granted.

modes.py: the five modes, each a sandbox and an approver. asks.py: which calls
ask (a plan, and whatever a tool says its call crosses). approval.py: the
approvers (the user, nobody). grants.py: what "don't ask again" granted, from
the log and the user's saved file. consent.py: whether the user's reply in
words is a plain yes. tool.py: `propose_plan`. The judge, the auto modes'
approver, is its own part (judge/).
"""

from settings import Setting

SETTINGS = (
    Setting("mode", "Default", "Mode", "default", "mode"),
    # Read at each check (the assembly's services), as the model's silence is
    Setting("consent_silence_seconds", "Limits",
            "Seconds the consent check may stay silent", 300, "number"),
)
