"""The asks that aren't the user's preferences: a plan (tool.py), and a call
that crosses what its tool says is asked about first (leaving the sandbox, a
fetch from a local address: the tool's `crosses`). Who answers is the mode's
approver (approval.py, judge/plugin.py); a grant can answer first
(grants.py).

The note they leave is named "rule", as in older logs, so those read the same.
"""

from core import tools
from core.hooks import Plugin

# A plan (tool.py) is always put to the approver; what it is about, shown to
# the user above the one question
PLAN = "propose_plan"
PLAN_ABOUT = (
    "ZERO proposes a plan. If you approve, its calls run without asking again "
    "until your next message."
)
# AskWhen's, for the tests
ASKED_ABOUT = "It asks first."


class Asks(Plugin):
    """Marks a plan, and a call that crosses what its tool says is asked
    about, for the mode's approver, in the tool's own words. Modes without a
    sandbox don't load it: in auto+ Permissions marks every call that needs
    permission, a plan included. `every_mode`: only a crossing its tool says
    asks in every mode (bypassPermissions, which asks nothing else)."""

    def __init__(self, every_mode=False):
        self.every_mode = every_mode

    def before_tool(self, call):
        crossing = tools.crossing(call.name, call.args)
        if self.every_mode:
            if crossing is None or not crossing.get("every_mode"):
                return None
        if call.name == PLAN:
            call.notes["rule"] = {
                "rule": "plan", "action": "ask", "reason": "a plan to approve",
                "about": PLAN_ABOUT,
            }
        elif crossing is not None:
            why = str(crossing["why"] or "").strip() or "no reason given"
            call.notes["rule"] = {
                "rule": crossing["rule"], "action": "ask",
                "reason": f"{tools.inside(crossing['fact'])}: {why.rstrip('.')}",
                "about": crossing["fact"],
            }
        return None


class AskWhen(Plugin):
    """Marks every call `when(call)` is true for, as the rules file once could:
    the tests' way to make a call ask (`make_session(ask_when=...)`)."""

    def __init__(self, when, reason="the call asks first"):
        self.when = when
        self.reason = reason

    def before_tool(self, call):
        if call.notes.get("rule") is None and self.when(call):
            call.notes["rule"] = {
                "rule": "ask_when", "action": "ask", "reason": self.reason,
                "about": ASKED_ABOUT,
            }
        return None
