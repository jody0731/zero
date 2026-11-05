"""The host kernel: the one process that runs every live session.

runtime.py: messages in, turns run, records out, crash repair, parked asks,
watchers, stop. sessions.py: the session list and what the user does to a
session (read, set, rename, archive, seen, delete). store.py: the one
database (sessions, inbox, asks). server.py: the socket and its messages.
parts.py: what a component's host part may add (jobs/host.py,
permissions/host.py): its table, its messages, its reply step. The kernel imports
no component and no assembly: host/main.py builds ZERO and hands the parts in.
"""

import settings
from settings import Setting

SETTINGS = (
    Setting("idle_minutes", "Host", "Minutes idle before the host leaves", 5,
            "number"),
)
# The kernel's own setting joins the components' (the assembly adds theirs)
settings.add(*SETTINGS)
