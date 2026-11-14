"""`zero mcp serve`'s own entry: the CLI execs this file in its place (as `zero
server` execs server/main.py), so the CLI never loads the MCP app and the MCP app
never loads the CLI.
"""

import sys
from pathlib import Path

if __name__ == "__main__":
    # Run as a script: ZERO's folder in place of this one, so that apps,
    # client and paths are ZERO's own
    folder = str(Path(__file__).resolve().parents[2])
    # With -I (ZERO's own Pythons), which leaves this one out, before
    # the rest
    if sys.flags.safe_path:
        sys.path.insert(0, folder)
    else:
        sys.path[0] = folder

from apps.mcp import server  # noqa: E402

if __name__ == "__main__":
    sys.exit(server.serve())
