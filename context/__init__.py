"""The context: what one model call sends (its system message, the tools and
the conversation), kept under a line so a session never hits a wall.

room.py: the context's size in tokens (the last call's count, an estimate of
what came since) and its room: the line, the headroom, a result's share.
compaction.py: making room, old outputs cleared to files and then, when
that isn't enough, a handoff for the older steps. results.py: the session's
folder of results saved whole. hooks.py: the check before every call, the
refusal net, and each result and notice fitted to its share (Context).
tool.py: `compact`, ZERO's own way to ask for a compaction. size_note.py:
the context's size told to ZERO each time it has grown by a mark (SizeNote).
"""

from settings import Setting

SETTINGS = (
    # The line a session's context is kept under; the model's window when
    # unset, and never past it
    Setting("compact_at_tokens", "Limits", "Compact at (tokens)", None, "int",
            minimum=16_000, unset_means="the model's context window"),
    # A result ZERO didn't size gets this share of the room left below the
    # line, or near it of the room a compaction leaves; past it, the whole
    # goes to a file (hooks.py). 15% measured at 7.3 (evals/
    # compaction_replay.py, 83 real sessions at lines of 32k to 262k): the
    # knee, past which each compaction more keeps few more results whole
    Setting("result_share_percent", "Limits",
            "Percent of the room left a result may take", 15, "int",
            maximum=100),
    # The next reply's room, its thinking included (447 real replies, 8 Nov:
    # p99 3,440 tokens, the longest 4,572), and a compaction handoff's (the
    # longest 2,335); each at most an eighth of the line, so a small window
    # keeps room for the talk (room.py)
    Setting("reply_room_tokens", "Limits", "Tokens kept for the next reply",
            8_000, "int", minimum=1_000),
    Setting("handoff_room_tokens", "Limits",
            "Tokens kept for a compaction's handoff", 8_000, "int",
            minimum=1_000),
    # A note of the context's size and of what a step counts for reading
    # it again, each time it has grown by this many tokens (size_note.py);
    # 0: none. On a large window no compaction comes to mark that a turn
    # has grown, and a number in `usage` waits to be asked for
    Setting("context_note_every_tokens", "Limits",
            "Tokens the context grows by between notes of its size", 100_000,
            "int", minimum=0),
)
