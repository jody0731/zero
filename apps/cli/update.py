"""`zero update`: this copy of ZERO to the newest release of the repository it
was cloned from.

An installed ZERO is a git clone with a release checked out. A release is a
tag, `v` and dotted numbers (v0.6.2.2), compared as numbers. The update fetches
the tags, checks out the newest, installs the requirements again when they
changed and prints the CHANGELOG sections it brought. It refuses rather than
merges: on local changes, and when the copy has commits of its own (Hermes'
updater stashes, rescues and repairs, and grew to some 11,600 lines). The
running host sees the new code and is replaced once it is idle.
"""

import re
import subprocess
import sys
from pathlib import Path

TAG = re.compile(r"v(\d+(?:\.\d+)*)")
SECTION = re.compile(r"## (\d+(?:\.\d+)*)")


def number(tag):
    """A release tag's numbers, to compare; None for any other tag."""
    match = TAG.fullmatch(tag)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def newest(tags):
    releases = [tag for tag in tags if number(tag) is not None]
    return max(releases, key=number, default=None)


def changes(notes, old, new):
    """The CHANGELOG's sections after release `old` (None: from the first)
    up to `new`, as the file orders them (newest first)."""
    low = number(old) if old else ()
    high = number(new)
    kept = []
    for section in re.split(r"(?m)^(?=## )", notes):
        match = SECTION.match(section)
        if match and low < number("v" + match.group(1)) <= high:
            kept.append(section.strip())
    return "\n\n".join(kept)


def name(tag):
    return tag[1:] if tag else "(no release)"


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True,
    )


def pip_install(requirements):
    """Install the requirements into the Python that runs this command: the
    clone's own .venv, which install.sh made."""
    command = [sys.executable, "-m", "pip", "install", "--quiet", "-r",
               str(requirements)]
    return subprocess.run(command).returncode


def update(root, out=print, pip=pip_install):
    """Exit code: 0 updated or already the newest, 1 nothing was updated."""
    root = Path(root)
    if not (root / ".git").exists():
        out(f"{root} is not an installed clone of ZERO; nothing was updated.")
        return 1
    changed = git(root, "status", "--porcelain").stdout.splitlines()
    if changed:
        out("ZERO's folder has local changes; nothing was updated:")
        for line in changed:
            out("  " + line[3:])
        return 1
    found = git(root, "describe", "--tags", "--abbrev=0", "--match", "v*")
    current = found.stdout.strip() if found.returncode == 0 else None
    fetched = git(root, "fetch", "--quiet", "--tags", "--force", "origin")
    if fetched.returncode:
        out(f"ZERO couldn't reach its repository: {fetched.stderr.strip()}")
        return 1
    latest = newest(git(root, "tag", "--list", "v*").stdout.split())
    if latest is None or (current and number(latest) <= number(current)):
        out(f"ZERO {name(current)} is the newest release.")
        return 0
    if git(root, "merge-base", "--is-ancestor", "HEAD", latest).returncode:
        out(f"ZERO's folder has commits of its own, not in {latest}; nothing "
            "was updated.")
        return 1
    requirements = root / "requirements.txt"
    before = requirements.read_bytes() if requirements.exists() else b""
    checked = git(root, "checkout", "--quiet", "--detach", latest)
    if checked.returncode:
        out(f"ZERO couldn't check out {latest}: {checked.stderr.strip()}")
        return 1
    out(f"Updated ZERO {name(current)} → {name(latest)}.")
    notes = root / "CHANGELOG.md"
    if notes.exists():
        brought = changes(notes.read_text(), current, latest)
        if brought:
            out("")
            out(brought)
            out("")
    after = requirements.read_bytes() if requirements.exists() else b""
    if after != before and pip(requirements):
        out(f"Its requirements didn't install; run {root / 'install.sh'} to "
            "try again.")
        return 1
    out("The host picks up the new code once it is idle.")
    return 0
