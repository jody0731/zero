"""The file tools: read, write and edit, in the session's box.

tool.py: the tools. helper.py: their hands, run through the session's box as a
command is. seen.py: what the tools saw of each file (from the log), for the
stale check and, on a resume, the files that changed while a turn was cut off.
"""

from settings import Setting

SETTINGS = (
    Setting("read_chars", "Limits", "Characters a file read returns at most",
            50_000, "int", least=1000),
)
