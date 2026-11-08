"""Where and when the agent is, so it doesn't spend steps finding out."""

import getpass
import platform
from datetime import datetime
from pathlib import Path

from core.hooks import Hook


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
