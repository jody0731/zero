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
)
