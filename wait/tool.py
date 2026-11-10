"""`wait`: this turn held until a notice or a message from the user comes,
or `minutes` pass.

What comes for a running turn waits in the server's inbox for its next step
(core/loop.py takes it in before each model call); the loop hands a call
`steers`, which lists what waits, and `stop`, the turn's stop flag. The
call returns as soon as the list is non-empty, saying only that something
came: the next step reads it, as it reads anything that comes mid-turn.
Nothing bounds `minutes`; the user's stop ends a held turn at once.
"""

import time

from core import history
from core.control import Stopped
from core.tools import tool

DESCRIPTION = (
    "Hold this turn until a notice or a message from the user comes, or "
    "`minutes` pass, whichever is first. No call to your model is made while "
    "it holds; the step after it reads the conversation again. What came is "
    "read at that step."
)
NOT_HERE = (
    "Nothing reaches a running turn in this session, so there is nothing to "
    "wait for."
)
CAME = "Held {took}: {what} came, read at the next step."
NOTHING = "Held {took}: nothing came."
# How often the inbox is looked at while a turn is held
LOOK_SECONDS = 0.5


def held_text(seconds):
    """How long a turn was held: "42 s", "2.5 min", "75 min"."""
    if seconds < 60:
        return f"{round(seconds)} s"
    if seconds < 600:
        return f"{seconds / 60:.1f} min"
    return f"{round(seconds / 60)} min"


def counted(count, one):
    return f"1 {one}" if count == 1 else f"{count} {one}s"


def came_text(steers):
    """What came, by kind: "2 notices", "1 message from the user", or both."""
    notices = sum(history.is_notice(steer) for steer in steers)
    messages = len(steers) - notices
    parts = []
    if notices:
        parts.append(counted(notices, "notice"))
    if messages:
        one = counted(messages, "message")
        parts.append(f"{one} from the user")
    return " and ".join(parts)


def check_minutes(args):
    minutes = args.get("minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
        return "minutes is a number"
    if minutes <= 0:
        return "minutes is above 0"
    return None


@tool(
    DESCRIPTION,
    summary=lambda args: f"wait: up to {args.get('minutes')} min",
    # Offered where a server carries the session: there notices reach a turn
    ready=lambda context: bool(context.get("on_a_server")),
    needs_permission=lambda args: False,
    validate=check_minutes,
    # The same call made again waits again
    changing=True,
    minutes=("number", "The longest it holds, in minutes"),
)
def wait(minutes, steers=None, stop=None):
    """`steers()`: what has come for the running turn and waits for its next
    step; `stop`: the turn's stop flag. Both from the loop, never the model."""
    if steers is None:
        return NOT_HERE
    started = time.monotonic()
    deadline = started + minutes * 60
    while True:
        found = steers()
        now = time.monotonic()
        if found:
            return CAME.format(took=held_text(now - started), what=came_text(found))
        if now >= deadline:
            return NOTHING.format(took=held_text(now - started))
        pause = min(LOOK_SECONDS, deadline - now)
        if stop is None:
            time.sleep(pause)
        elif stop.wait(pause):
            raise Stopped()
