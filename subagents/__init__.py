"""Subagents (6.7): a session ZERO starts under the one it works in, a means
it picks (principle 3), which reports back through the inbox.

tool.py: `start_subagent`, `send_message`, `subagents`, and the texts the
model reads of them (a subagent's task, its reports, a message between
sessions). hooks.py: PastEstimate, the one notice to a parent when its
subagent's use passes the estimate it was started with. service.py: what
subagents are in the server: the session started (a fork of the parent's
finished turns, or a handoff), its task queued, what it didn't send passed
up to its parent, the session tree, messages by id, a stop.
store.py: their table.

A session tree is a session the user started and the subagents under it;
a fork the user makes of a subagent starts a tree of its own. Nobody
watches a subagent: in default mode it runs as auto, the judge answering
what would ask the user, with the user's words anywhere in its tree as the
goal (the assembly; judge/hooks.py).
"""

from settings import Setting

SETTINGS = (
    # Levels of subagents below a session the user started; 0: none, the
    # tool offered nowhere. Breadth has no cap. Read at each use, as every
    # limit is, so a change reaches every session, open ones too
    Setting("max_subagent_depth", "Limits", "Subagents depth", 2, "int",
            minimum=0),
)
