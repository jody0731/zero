"""Where and when the agent is, so it doesn't spend steps finding out."""

import getpass
import platform
from datetime import datetime
from pathlib import Path

from core.hooks import Hook

# A restart gives each session a new scratch folder (sandbox/sandbox.py's
# LASTS), while its system message keeps the one it was written with
NEW_SCRATCH = (
    "[Harness note: ZERO restarted, and this session's scratch folder, "
    "$TMPDIR, is now {path}, new and empty.]"
)


class Environment(Hook):
    def __init__(self, sandbox, mode=None):
        # The session's own sandbox: what its commands can touch
        self.sandbox = sandbox
        # The mode and who answers its asks (permissions/modes.py), handed in
        # by the assembly; None leaves the line out
        self.mode = mode

    def _mode_line(self):
        return f"\n- {self.mode}" if self.mode else ""

    def system_prompt(self, session):
        now = datetime.now().astimezone()
        return (
            "\n\nEnvironment (when this system message was written):\n"
            f"- user: {getpass.getuser()}; home: {Path.home()}\n"
            f"- working directory (the workspace): {self.sandbox.workspace}\n"
            f"- {self.sandbox.environment_line()}{self._mode_line()}\n"
            f"- system: {os_name()}, kernel {platform.release()}\n"
            f"- date: {now:%A %d %B %Y, %H:%M %Z}"
        )

    def turn_start(self, turn):
        """The scratch folder, said again when the context no longer names
        the one the session has: after a restart."""
        private_tmp = getattr(self.sandbox, "private_tmp", None)
        if private_tmp is None:
            return
        path = private_tmp()
        session = turn.session
        if any(path in str(m.get("content") or "") for m in session.messages):
            return
        session.write("note", kind="new_scratch_folder",
                      text=NEW_SCRATCH.format(path=path))

    def switched(self, session):
        """After a change of mode, model or folder: where commands now
        run and who answers their asks, for the switch note."""
        return (
            f"\n- working directory (the workspace): {self.sandbox.workspace}"
            f"\n- {self.sandbox.environment_line()}{self._mode_line()}"
        )


def os_name():
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return platform.system()
