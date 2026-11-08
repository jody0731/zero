"""The context: what one model call sends (its system message, the tools and
the conversation), kept under a line so a session never hits a wall.

room.py: the context's size in tokens (the last call's count, an estimate of
what came since) and its room: the line, the headroom, a result's share.
compaction.py: making room, old outputs cleared to files and then, when
that isn't enough, a handoff for the older steps. results.py: the session's
folder of results saved whole. hooks.py: the check before every call, the
refusal net, and each result and notice fitted to its share (Context).
tool.py: `compact`, ZERO's own way to ask for a compaction.
"""

from settings import Setting

SETTINGS = (
    # The line a session's context is kept under; the model's window when
    # unset, and never past it
    Setting("compact_at_tokens", "Limits", "Compact at (tokens)", None, "int",
            minimum=16_000, unset_means="the model's context window"),
)
