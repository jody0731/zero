"""What `server/hello` tells a client: the protocol it speaks and the code it runs.

The protocol is a number: adding a message, a field or an event keeps it,
removing one or changing its meaning bumps it. Every request names the
protocol it is written in (none: 4, the last before requests named it), and
the server answers in it, from OLDEST to its own, its replies walked back one
step at a time (protocol/older.py); so an app keeps working across a server's
update, and a client never has to stop a newer server to talk to it.

The release is ZERO's version, its CHANGELOG's newest section: a client
restarts an idle server of an older release (or of the same release on other
code, a tree being worked on), never a newer one. The build is a hash of the files
that ship (`ships`: the code, its templates, catalog, prices and schema) and of
the Python that runs them (its version line, which says free-threaded), taken
when a process starts, so a client can tell a server that runs older code, or
the same code on another Python (a clone's server, once ZERO has its own). An
installed copy's own .venv, git's folder and the caches are never part of it,
so a pip upgrade is no new build; nor is dev/, which never ships
(dev/release.py exports by the same rule).
"""

import hashlib
import os
import re
import sys
from pathlib import Path

PROTOCOL = 5
# The oldest protocol the server still answers in: 4 is what a request
# naming none is written in
OLDEST = 4
# A CHANGELOG section's heading: the release it is for
RELEASE_HEADING = re.compile(r"(?m)^## (\d+(?:\.\d+)*)\b")
# ZERO's folder
ROOT = Path(__file__).resolve().parents[1]
# What stays on the development side: the tests and evals, and the records of
# how the generation was built
LEFT_OUT = ("dev", "DESIGN.md", "CHANGES.md")
# What ships, by suffix or by name
SUFFIXES = (".py", ".toml", ".md", ".txt", ".sh", ".json", ".svg", ".html",
            ".css", ".js")
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
    """A short hash of the files that ship and the Python that runs them."""
    root = Path(root)
    digest = hashlib.sha256(sys.version.encode() + b"\0")
    for relative in shipped_files(root):
        digest.update(relative.as_posix().encode() + b"\0"
                      + (root / relative).read_bytes() + b"\0")
    return digest.hexdigest()[:12]


def release(root=ROOT):
    """ZERO's release this code is, its CHANGELOG's newest section; "" for
    a copy without one."""
    try:
        found = RELEASE_HEADING.search((Path(root) / "CHANGELOG.md").read_text())
    except OSError:
        return ""
    return found.group(1) if found else ""


def release_order(text):
    """A release as numbers to compare ("0.7.6.1" → (0, 7, 6, 1)); () for
    none."""
    try:
        return tuple(int(part) for part in text.split(".")) if text else ()
    except ValueError:
        return ()
