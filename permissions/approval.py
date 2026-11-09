"""Who answers when a rule says "ask": one approver per mode, on the `approve` hook.

The loop calls `approve` only after every check passed, and these act only on
calls the rules hook marked "ask"; anything else runs. Modes (modes.py),
least to most permissive: readOnly (Refuse), default (Approval, or Refuse with
nobody there), auto and auto+ (the judge, judge/), bypassPermissions
(none: no rules are loaded). auto+ has no rules either: Permissions marks each
call that needs permission, and the judge answers.

Work nobody watches (6.7) raises default's approver to the judge, never the
sandbox: a subagent ZERO started runs default as auto (the assembly), and in
a session of the user's own a round's asks go to the judge (DefaultApproval).
readOnly is never raised.
"""

from core import tools
from core.asking import DECLINED, PARK, ask_of, put_to_user
from core.hooks import Deny, Park, Hook

REFUSED = "Not run: a rule says to ask first ({reason}), and nobody is here to answer."
READ_ONLY = (
    "Not run: a rule says to ask first ({reason}), and this session's mode, "
    "readOnly, asks nothing."
)


def ask_user(call, why):
    """Anything but y/yes is a no, end of input included."""
    print(f"  ({why})")
    try:
        answer = input("  Run it? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    return answer in ("y", "yes")


class Refuse(Hook):
    """readOnly, or default with nobody there: every ask is a no, so a run gives
    the same answer every time."""

    def __init__(self, text=REFUSED):
        # READ_ONLY in readOnly, where someone may be there but isn't asked
        self.text = text

    def approve(self, call):
        hit = ask_of(call)
        if hit:
            return Deny(self.text.format(reason=hit["reason"]))


# auto+ has no rules: each call that needs permission asks, and the judge
# answers; the reason is the judge's and the log's, never shown to a user
NEEDS_PERMISSION = "the call needs permission"


class PermissionAsks(Hook):
    """auto+: every call that needs permission (core/tools.py,
    needs_permission) is marked to ask, as a rule would mark it."""

    def before_tool(self, call):
        if tools.needs_permission(call.name, call.args):
            call.notes["permission"] = {
                "rule": "permission", "action": "ask", "reason": NEEDS_PERMISSION,
            }
        return None


class UserApproval(Hook):
    """default: the user answers y/n."""

    def __init__(self, ask=ask_user):
        self._ask = ask

    def approve(self, call):
        hit = ask_of(call)
        if not hit:
            return None
        if self._ask is PARK:
            return Park(f"rule: {hit['reason']}", hit.get("about"))
        answer = put_to_user(call, self._ask, f"rule: {hit['reason']}")
        if answer is None:
            return Deny(REFUSED.format(reason=hit["reason"]))
        if not answer:
            return Deny(DECLINED)


class DefaultApproval(Hook):
    """default's approver: the user, except in work nobody watches (a round
    of work in rounds), where the judge answers what would have asked the
    user. `user` and `judge`: their approvers' hooks (UserApproval, the
    judge's); `unattended(turn)`: whether nobody watches this turn."""

    def __init__(self, user, judge, unattended):
        self.user = user
        self.judge = judge
        self.unattended = unattended

    def turn_start(self, turn):
        self.judge.turn_start(turn)

    def approve(self, call):
        if self.unattended(call.turn):
            return self.judge.approve(call)
        return self.user.approve(call)

    def after_tool(self, call):
        self.judge.after_tool(call)

    def step_end(self, step):
        if self.unattended(step.turn):
            return self.judge.step_end(step)
        return None
