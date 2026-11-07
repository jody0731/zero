"""What `server/hello` tells a client: the protocol it speaks and the code it runs.

The protocol is a number: adding a message, a field or an event keeps it,
removing one or changing its meaning bumps it. The build is a hash of ZERO's
own source, taken when a process starts, so a client can tell a server that runs
older code.
"""

import hashlib
from pathlib import Path

PROTOCOL = 3
# ZERO's folder
ROOT = Path(__file__).resolve().parents[1]


def build_hash(root=ROOT):
    """A short hash of the harness's code: its Python files, not dev/'s
    tests and evals."""
    digest = hashlib.sha256()
    files = root.rglob("*.py")
    for path in sorted(files):
        relative = path.relative_to(root)
        if relative.parts[0] == "dev" or "__pycache__" in relative.parts:
            continue
        digest.update(str(relative).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:12]
