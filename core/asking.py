"""The ask: a call that waits for the user's answer, and what the model
reads of that answer.

The contract between the hooks that want an answer (the rules and the caps
mark a call's notes), the approvers that put the question (permissions/,
judge/hooks.py) and the loop, which parks a turn on the ask and resumes it on
the reply (core/loop.py). Kept apart from all three, so no hook imports
another and the loop holds no approver's words.
"""

import time

# Who a call that needs an approver goes to, as each tool tells the model:
# the mode line (permissions/modes.py) names them, readOnly's no one. "It is
# asked about first" read as the user being asked, in every mode
TO_APPROVER = "the session's approver, whom the environment's mode line names"
DECLINED = "Declined: the user said no to this command."
# The user's "No" at the box: the call isn't run and the turn ends
DECLINED_STOP = (
    "Declined: the user said no to this command, and the turn ended there."
)
APPROVED = "(The user approved this.)"
APPROVED_LATE = "(The user approved this {waited} after you asked.)"
CANCELLED = (
    "Not run: the user replied with a message instead of approving; called "
    "again, it is asked about again."
)
NOT_ASKED = (
    "Not run: the turn was waiting for the user's answer about an earlier call, "
    "and they replied with a message instead."
)


# The call's own asks, which a grant in the permissions memory answers, and
# the caps', which it never does
CALL_ASKS = ("rule", "permission")
LIMIT_ASKS = ("limit",)
# The asks only the user answers, in every mode (rounds/hooks.py, Budget):
# the mode's approver leaves them alone
USER_ASKS = ("budget",)
# What the user may answer at the box: "session" and "saved" only when
# the call's ask has a grant to offer (permissions/grants.py)
YES, SESSION, SAVED, NO = "yes", "session", "saved", "no"
CHOICES = (YES, SESSION, SAVED, NO)


def ask_of(call):
    """The ask that wants an answer (permissions/asks.py, auto+'s Permissions
    in permissions/approval.py, the jobs' cap in jobs/hooks.py), or None;
    several at once ask once, with every reason. A call the permissions
    memory grants (permissions/grants.py) asks only for its caps."""
    return waiting_ask(call.notes)


def waiting_ask(notes, granted=True):
    """ask_of() from a call's notes; with granted=False, as if nothing were
    granted. A user's ask (USER_ASKS) isn't among them: its hook asks it."""
    names = CALL_ASKS + LIMIT_ASKS
    if granted and notes.get("granted") is not None:
        names = LIMIT_ASKS
    hits = [notes.get(name) for name in names]
    hits = [hit for hit in hits if hit and hit["action"] == "ask"]
    if len(hits) > 1:
        abouts = [h["about"] for h in hits if h.get("about")]
        return {**hits[0], "reason": "; and ".join(h["reason"] for h in hits),
                "about": " ".join(abouts) or None}
    return hits[0] if hits else None


class _Park:
    """Passed as `ask` by a server that parks a turn on disk until the user replies
    (the server): approvers return Park instead of waiting for an answer."""

    def __repr__(self):
        return "PARK"


PARK = _Park()


def choices(notes):
    """The answers an ask offers: a grant's two only when the call's own ask
    is all that asks and it has one to offer."""
    if notes.get("offer") and not any(notes.get(name)
                                      for name in LIMIT_ASKS + USER_ASKS):
        return list(CHOICES)
    return [YES, NO]


def put_to_user(call, ask, why):
    """Pass on why, then ask. The wait is the user's time, not the
    agent's (a turn's budget counts agent time)."""
    t0 = time.monotonic()
    try:
        return ask(call, why)
    finally:
        call.turn.waiting += time.monotonic() - t0
