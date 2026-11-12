"""Whether the sandbox can start on this machine, checked by starting it (design
rule 8): `zero doctor` asks at install and on demand, and a sandboxed command
that bwrap itself fails to start asks again (shell/tool.py), so what is wrong
reaches the model and the user with its known cause.
"""

import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from sandbox.sandbox import Sandbox, SandboxError

# How long the check's command may take; a check older than this many seconds
# is asked again, rather than every failed command running one
CHECK_SECONDS = 10
CHECK_KEPT_SECONDS = 60
# What bwrap prints before its own errors, as against the command's
BWRAP_SAYS = "bwrap: "
# The causes known from what bwrap says: words in its message → the cause
USER_NAMESPACES = (
    "user namespaces, which bwrap needs, are restricted on this machine "
    "(Ubuntu 23.10 and later restrict them through AppArmor; ZERO's README "
    "shows how to lift it)"
)
CAUSES = (
    (("uid map", "new namespace", "namespace failed", "RTM_NEWADDR"),
     USER_NAMESPACES),
    (("Can't mount proc",),
     "the system won't let bwrap mount /proc, as in a container without the "
     "rights to"),
)
# What a command's result gets when bwrap itself couldn't start it and the
# check finds why (shell/tool.py)
CANT_START = ("(The sandbox can't start on this machine: {why}. `zero doctor` "
              "checks it.)")
# One check at a time, and the last one's (time, finding)
CHECKING = threading.Lock()
LAST = {"at": None, "found": None}


def check_sandbox():
    """None when a command runs in the sandbox here; else what is wrong, in a
    sentence, with the known cause where there is one."""
    workspace = Path(tempfile.mkdtemp(prefix="zero-check-"))
    try:
        box = Sandbox(workspace)
        try:
            argv = box.command(["true"])
        except SandboxError as error:
            return str(error)
        tries = 0
        while True:
            tries += 1
            try:
                done = subprocess.run(
                    argv, env=box.env(), cwd=workspace,
                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                    timeout=CHECK_SECONDS,
                )
            except subprocess.TimeoutExpired:
                return f"a command in it didn't end in {CHECK_SECONDS} s"
            except OSError as error:
                return f"bwrap didn't run: {error}"
            # A path to cover gone since the look says nothing of the machine
            if not box.start_again(tries, done.returncode, done.stderr):
                break
            argv = box.command(["true"])
        if done.returncode == 0:
            return None
        said = (done.stderr + done.stdout).strip().splitlines()
        return cause_of(said[0] if said else f"exit code {done.returncode}")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def cause_of(message):
    """bwrap's message, with its known cause after it."""
    message = message.removeprefix(BWRAP_SAYS).strip()
    for words, cause in CAUSES:
        if any(word in message for word in words):
            return f"bwrap says {message!r}: {cause}"
    return f"bwrap says {message!r}"


def failed_to_start(exit_code, output_head):
    """Whether a sandboxed command's result is bwrap's own failure to start
    it, not the command's: bwrap's exit code 1 and its message first."""
    return exit_code == 1 and output_head.startswith(BWRAP_SAYS)


def why_it_cant_start(now=None):
    """check_sandbox's finding, asked again once CHECK_KEPT_SECONDS passed:
    each failed command doesn't start a check of its own."""
    now = time.monotonic() if now is None else now
    with CHECKING:
        if LAST["at"] is None or now - LAST["at"] > CHECK_KEPT_SECONDS:
            LAST["found"] = check_sandbox()
            LAST["at"] = now
        return LAST["found"]
