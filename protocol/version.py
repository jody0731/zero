"""What `server/hello` tells a client: the protocol it speaks and the code it runs.

The protocol is a number: adding a message, a field or an event keeps it,
removing one or changing its meaning bumps it. The build is a hash of the files
that ship (`ships`: the code, its templates, catalog, prices and schema), taken
when a process starts, so a client can tell a server that runs older code. An
installed copy's own .venv, git's folder and the caches are never part of it,
so a pip upgrade is no new build; nor is dev/, which never ships
(dev/release.py exports by the same rule).
"""

import hashlib
import os
from pathlib import Path

PROTOCOL = 4
# ZERO's folder
ROOT = Path(__file__).resolve().parents[1]
# What stays on the development side: the tests and evals, and the records of
# how the generation was built
LEFT_OUT = ("dev", "DESIGN.md", "CHANGES.md")
# What ships, by suffix or by name
SUFFIXES = (".py", ".toml", ".md", ".txt", ".sh", ".json", ".svg")
NAMES = (".gitignore", "LICENSE")
# Folders that are never ZERO's code: an installed copy's venv, git's own,
# Python's caches
NOT_CODE = (".venv", ".git", "__pycache__")


def ships(relative):
    """Whether a file of ZERO's folder (its path relative to it) ships."""
    relative = Path(relative)
    if relative.parts[0] in LEFT_OUT or set(relative.parts) & set(NOT_CODE):
        return False
    return relative.name in NAMES or relative.suffix in SUFFIXES


def shipped_files(root=ROOT):
    """The files of ZERO's folder that ship, relative to it, sorted."""
    root = Path(root)
    found = []
    for folder, folders, files in os.walk(root):
        here = Path(folder).relative_to(root)
        folders[:] = [name for name in folders if name not in NOT_CODE
                      and not (here == Path(".") and name in LEFT_OUT)]
        found += [here / name for name in files if ships(here / name)]
    return sorted(found)


def build_hash(root=ROOT):
    """A short hash of the files that ship."""
    root = Path(root)
    digest = hashlib.sha256()
    for relative in shipped_files(root):
        digest.update(relative.as_posix().encode() + b"\0"
                      + (root / relative).read_bytes() + b"\0")
    return digest.hexdigest()[:12]
