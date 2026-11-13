"""What the server keeps of one session while it runs, the session's own.

7.5.2, parallel by design: each session owns its state, behind its own lock,
which nothing of another session takes; the runtime keeps only the table of
them, behind one door held for a lookup and a count, never for a query, a
commit or a turn (server/runtime.py). A session's lock may wait on its own
row's commit (an admission, a turn's end), which only that session's other
parts wait for.
"""


class SessionState:
    def __init__(self, lock):
        # This session's lock: whether a turn of it runs, and which
        self.lock = lock
        # The thread holding the session as its runner (its turns' runner, a
        # compaction, a rewind, a delete), or None
        self.runner = None
        # Its threads past their last turn with their last word still to say
        # (`settled`, then a dispatch)
        self.leaving = 0
        # The turn running: (its Control, its inbox row), or None
        self.running = None
        # Left to the next start's repair: another server holds its lock, or
        # its log or its database failed
        self.blocked = False
        # The harness built for it, kept while the server runs
        self.harness = None
        # Its watching connections, token → Watcher: replaced whole under
        # the lock, never changed in place, so a publish reads it without
        # the lock
        self.watchers = {}
        # The reply streaming in: its latest draft event, or None
        self.draft = None
        # Its context meter as its watchers last got it, and the line it was
        # measured under (the runtime's meter_line)
        self.meter = None
        self.meter_line = None
