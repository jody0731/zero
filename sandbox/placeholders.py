"""Read-only stand-ins for what a later session trusts, while a sandboxed
command runs where it is missing: a workspace's ZERO.md (read as the project's
instructions) and its .git (whose hooks run outside the sandbox at the next
commit). bwrap can only keep a path read-only that exists, so this runs
bwrap: it makes an empty file or folder at each missing path, marked as a
stand-in, holds it while the command runs, and removes it after, unless
another command still holds it. Codex's way (its bwrap's synthetic mount
targets).

Run by path, `python -I placeholders.py STAND_INS -- BWRAP...`, before ZERO's
modules load: it imports the standard library only. A stand-in a killed
command left goes at the next command that holds it.
"""

import ctypes
import fcntl
import json
import os
import signal
import subprocess
import sys

# What marks a stand-in, so a real empty file is never taken for one
MARK = "user.zero.placeholder"


def mark(path):
    """Mark a stand-in; a file system without user attributes keeps none, and
    then only the command that made it removes it."""
    try:
        os.setxattr(path, MARK, b"1", follow_symlinks=False)
    except OSError:
        pass


def is_marked(path):
    try:
        return os.getxattr(path, MARK, follow_symlinks=False) == b"1"
    except OSError:
        return False


def make(path, kind):
    """The stand-in at path, made now or found (another command's, or one a
    killed command left); its handle, held shared while the command runs.
    None when a real file is there: it is kept read-only as it is."""
    made = False
    try:
        if kind == "folder":
            os.mkdir(path, 0o555)
        else:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDONLY, 0o444))
        made = True
        mark(path)
    except FileExistsError:
        pass
    if not made and not is_marked(path):
        return None
    handle = os.open(path, os.O_RDONLY)
    fcntl.flock(handle, fcntl.LOCK_SH)
    return handle, made


def empty(path):
    try:
        if os.path.isdir(path):
            return not os.listdir(path)
        return os.path.getsize(path) == 0
    except OSError:
        return False


def remove(path, handle, made):
    """Remove a stand-in no other command holds, if it is still empty."""
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(handle)
        return
    try:
        if (made or is_marked(path)) and empty(path):
            if os.path.isdir(path):
                os.rmdir(path)
            else:
                os.unlink(path)
    except OSError:
        pass
    finally:
        os.close(handle)


# Linux's prctl option that signals a process when its parent (the thread that
# started it) ends: bwrap's --die-with-parent, which this runner now stands
# between
PR_SET_PDEATHSIG = 1


def die_with_parent():
    """End with the thread that started this, as bwrap would have."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except (OSError, AttributeError):
        pass


def main(argv):
    die_with_parent()
    stand_ins = json.loads(argv[1])
    command = argv[argv.index("--") + 1:]
    held = []
    for path, kind in stand_ins:
        found = make(path, kind)
        if found is not None:
            held.append((path, *found))
    child = subprocess.Popen(command)

    def forward(number, frame):
        child.send_signal(number)

    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, forward)
    code = child.wait()
    for path, handle, made in held:
        remove(path, handle, made)
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
