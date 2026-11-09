"""Session search: a search of what every session's log holds, running or
done.

index.py: the index, SQLite's full-text search (FTS5) in a file of its own
beside the server's database, caught up from the logs at each search.
search.py: its three uses (the sessions that match; one session's matching
records; the records near one). tool.py: `session_search`, ZERO's.
service.py: hands each session its search and answers `session/search`, the
user's and other agents' (`zero session search`). No settings of its own.
"""
