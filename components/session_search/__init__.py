"""Session search: a search of what every session's log holds, running or
done.

index.py: the index, SQLite's full-text search (FTS5) in a file of its own
beside the server's database, caught up from the logs at each search.
search.py: its three uses (the sessions that match; one session's matching
records; the records near one). tool.py: `session_search`, ZERO's.
service.py: hands each session its search and answers `session/search`, the
user's and other agents' (`zero session search`).
"""

from settings import Setting

SETTINGS = (
    # ZERO's `count` sets the first two at each call; these hold when it
    # names none (search.py)
    Setting("session_search_count", "Limits",
            "Sessions or records a search gives when ZERO names no count", 10,
            "int"),
    Setting("session_search_each_side", "Limits",
            "Records on each side when a search reads near one", 5, "int",
            minimum=0),
    # FTS5's snippet() shows 64 words at most (index.py)
    Setting("session_search_snippet_words", "Limits",
            "Words in a match's snippet", 32, "int", maximum=64),
)
