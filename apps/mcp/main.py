"""`zero mcp serve`'s own entry: the CLI execs this file in its place (as `zero
host` execs host/main.py), so the CLI never loads the MCP app and the MCP app
never loads the CLI.
"""

import sys
from pathlib import Path

if __name__ == "__main__":
    # Run as a script: ZERO's folder in place of this one, so that apps,
    # client and paths are ZERO's own
    sys.path[0] = str(Path(__file__).resolve().parents[2])

from apps.mcp import server  # noqa: E402

if __name__ == "__main__":
    sys.exit(server.serve())
