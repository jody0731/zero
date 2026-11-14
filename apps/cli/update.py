"""`zero update`: this copy of ZERO to the newest release.

An install (7.5.3 on) is a release folder in ZERO's install folder: the
update asks where its release.json says releases come from for the newest,
fetches its archive for this machine, tries it and switches to it whole or
not at all (install_folder.py), then prints the CHANGELOG sections it
brought. An installed git clone, from before 7.5.3, moves to the newest
release's archive the same way (on its first start the new code moves it to
its own release: move_from_clone.py). A clone whose release has no archive
for its machine, or that was never installed, updates as clones did: it
fetches the tags, checks out the newest, installs the requirements again
when they changed, and refuses rather than merges, on local changes and when
the copy has commits of its own (Hermes' updater stashes, rescues and
repairs, and grew to some 11,600 lines). A release is a tag, `v` and dotted
numbers (v0.6.2.2), compared as numbers. The running server sees the new
code and is replaced once it is idle.
"""

import re
import subprocess
import sys
from pathlib import Path

from apps.cli import move_from_clone, releases
from apps.cli.install_folder import (
    Busy, InstallError, InstallFolder, is_release_folder_in, release_file,
)
from paths import CODE_FOLDER, changelog_version, release_folder

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


SERVER_NOTE = "The server picks up the new code once it is idle."


def update(root, out=print, pip=pip_install):
    """Exit code: 0 updated or already the newest, 1 nothing was updated."""
    root = Path(root)
    release = release_folder(root)
    if release is not None:
        return update_release(release, out)
    command = move_from_clone.installed_clone(root)
    if command is not None:
        moved = update_clone_by_archive(root, command, out)
        if moved is not None:
            return moved
    return update_checkout(root, out, pip)


def brought(code, old, new, out):
    """The CHANGELOG sections a release brought, from its own CHANGELOG."""
    notes = Path(code) / "CHANGELOG.md"
    if notes.exists():
        text = changes(notes.read_text(), old, new)
        if text:
            out("")
            out(text)
            out("")


def update_release(release, out):
    """An install: the newest release's archive, switched to."""
    target = InstallFolder(release.parent.parent)
    if not is_release_folder_in(release, target):
        out(f"{release} isn't in an install folder; nothing was updated.")
        return 1
    found = release_file(release)
    current = target.current_version() or found.get("version")
    place = releases.source(found.get("source"))
    if place is None or current is None:
        out(f"{release}'s release.json says no version or no place releases "
            "come from; nothing was updated.")
        return 1
    try:
        latest = releases.newest(place)
    except releases.ReleaseError as error:
        out(f"ZERO couldn't find its newest release: {error}")
        return 1
    if releases.number(latest) <= releases.number(current):
        out(f"ZERO {current} is the newest release.")
        return 0
    try:
        with target.locked():
            target.sweep()
            archive = releases.fetch(place, latest, target.scratch(), out)
            target.add(target.unpack(archive), latest, out)
    except (Busy, InstallError, releases.ReleaseError, OSError) as error:
        out(f"ZERO wasn't updated: {error}. ZERO {current} runs as before.")
        return 1
    out(f"Updated ZERO {current} → {latest}.")
    brought(target.release(latest) / CODE_FOLDER, f"v{current}", f"v{latest}",
            out)
    out(SERVER_NOTE)
    return 0


def update_clone_by_archive(root, command, out):
    """An installed clone moved to the newest release's archive; None where
    that release has none for this machine, or none is found (the checkout
    then, as before)."""
    place = releases.source(
        releases.source_from_origin(move_from_clone.origin(root) or ""))
    if place is None:
        return None
    try:
        latest = releases.newest(place)
    except releases.ReleaseError:
        return None
    current = changelog_version(root / "CHANGELOG.md")
    try:
        move_from_clone.move(root, command, InstallFolder(), latest, out)
    except releases.NoArchive:
        return None
    except (Busy, InstallError, releases.ReleaseError, OSError) as error:
        out(f"ZERO wasn't updated: {error}. It runs from {root} as before.")
        return 1
    out(f"Updated ZERO {current} → {latest}.")
    brought(InstallFolder().release(latest) / CODE_FOLDER,
            f"v{current}" if current else None, f"v{latest}", out)
    out(SERVER_NOTE)
    return 0


def update_checkout(root, out=print, pip=pip_install):
    """A clone: the newest tag checked out."""
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
    out(SERVER_NOTE)
    return 0
