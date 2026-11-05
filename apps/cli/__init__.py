"""The command line, `zero`: main.py reads the words and runs what they name.
The plain commands (commands.py) and install, uninstall and update (update.py)
run in its process; the full screen (apps/tui/), `zero mcp serve` (apps/mcp/)
and the host (host/main.py) are execs of their own entries, so the command line
imports none of them. It reaches ZERO only through client/.
"""
