"""The server kernel: the one process that runs every live session.

runtime.py: messages in, turns run, records out, crash repair, parked asks,
watchers, stop. session_actions.py: the session list and what the user does
to a session (read, set, rename, archive, mark seen, delete). store.py: the one
database (sessions, inbox, asks). server.py: the socket and its messages.
services.py: what a component's server service may add (jobs/service.py,
permissions/service.py): its table, its messages, its reply step. The kernel imports
no component and no assembly: server/main.py builds ZERO and hands the services in.
"""

import settings
from settings import Setting

SETTINGS = (
    Setting("server_idle_minutes", "Server", "Minutes idle before the server leaves", 5,
            "number"),
    # Each of the user's messages a rewind's or a fork's note lists shows its
    # first line, this long at most; the rest "… (the whole in the log)"
    # (core/history.py's part_lines)
    Setting("rewind_note_line_chars", "Limits",
            "Characters of each message a rewind's or fork's note shows", 200,
            "int", minimum=10),
)
# The kernel's own setting joins the components' (the assembly adds theirs)
settings.add(*SETTINGS)
