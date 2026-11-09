"""Limits: why a turn stops or has a call refused.

stops.py: what ends a turn with its reason (malformed replies in a row).
repeats.py: a call made again soon isn't run. A full context ends nothing:
it is compacted (context/). How long a turn runs and what it spends is its
budget, the user's (rounds/).
"""

from settings import Setting

SETTINGS = (
    Setting("max_broken_replies", "Limits", "Malformed replies that stop a turn",
            3, "int"),
    # After this the same call runs again: the world it looks at may have
    # changed (a download grew, a service came up)
    Setting("repeat_after_seconds", "Limits",
            "Seconds before the same call runs again", 30, "number"),
)
