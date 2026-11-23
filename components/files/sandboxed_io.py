"""The file tools' hands: load a file's bytes, or store new ones.

Run by tool.py through the session's own sandbox, as bash's commands
are (`python -I -c` with this file's source; a JSON request on stdin, a JSON
answer on stdout), so the sandbox's walls hold for files as for commands: one
wall, as Codex runs apply_patch. Only the standard library: the sandbox may
hide this folder and the venv (the evals hide the whole repository).
Everything the tools decide (ranges, matches, the stale check) is decided
outside, in tool.py; this only touches the disk.
"""

import base64
import hashlib
import json
import os
import secrets
import sys


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_target(path):
    """The absolute path, links followed: a write through a link lands where
    the link points, and a wall there stops it."""
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.lexists(path):
        return os.path.realpath(path)
    parent, name = os.path.split(path)
    return os.path.join(os.path.realpath(parent), name)


def kind(path):
    if not os.path.lexists(path):
        return "missing"
    if os.path.isdir(path):
        return "directory"
    if not os.path.isfile(path):
        return "other"
    return "file"


def load(request):
    """A file's bytes, or what it is instead: missing, a directory, something
    that isn't a regular file, or bigger than `cap`."""
    path = write_target(request["path"])
    found = kind(path)
    answer = {"path": path, "kind": found}
    if found != "file":
        return answer
    size = os.path.getsize(path)
    answer["size"] = size
    if size > request["cap"]:
        answer["kind"] = "too_big"
        return answer
    with open(path, "rb") as f:
        data = f.read()
    answer["sha"] = sha(data)
    answer["data"] = base64.b64encode(data).decode()
    return answer


def store(request):
    """Write the bytes whole, if the file is as `expect` says: None means it
    doesn't exist, a hash means it has that content, "any" skips the check.
    Written beside it, then moved into place, so a reader never sees half a
    file; the old file's permissions are kept."""
    path = write_target(request["path"])
    found = kind(path)
    expect = request["expect"]
    if found not in ("missing", "file"):
        return {"path": path, "kind": found}
    if expect != "any":
        current = None
        if found == "file":
            with open(path, "rb") as f:
                current = sha(f.read())
        if current != expect:
            return {"path": path, "kind": found, "changed": True}
    data = base64.b64decode(request["data"])
    folder, name = os.path.split(path)
    os.makedirs(folder, exist_ok=True)
    draft = os.path.join(folder, f".{name}.zero-{secrets.token_hex(4)}")
    try:
        with open(draft, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if found == "file":
            os.chmod(draft, os.stat(path).st_mode & 0o7777)
        os.replace(draft, path)
    except BaseException:
        if os.path.lexists(draft):
            os.unlink(draft)
        raise
    return {"path": path, "kind": "file", "sha": sha(data)}


def main():
    request = json.load(sys.stdin)
    try:
        answer = {"load": load, "store": store}[request["op"]](request)
    except OSError as e:
        answer = {"error": e.strerror or str(e), "errno": e.errno}
    json.dump(answer, sys.stdout)


if __name__ == "__main__":
    main()
