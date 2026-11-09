"""Where and when the agent is, so it doesn't spend steps finding out."""

import getpass
import platform
from datetime import datetime
from pathlib import Path

from core import history
from core.hooks import Hook

# A restart gives each session a new scratch folder (sandbox/sandbox.py's
# LASTS), while its system message keeps the one it was written with
NEW_SCRATCH = (
    "[Harness note: ZERO restarted, and this session's scratch folder, "
    "$TMPDIR, is now {path}, new and empty.]"
)
# A fork's first turn: whose fork it is and what differs from that session;
# its system message is the parent's byte for byte, so the cache holds
FORKED = (
    "[This session is a fork of session {session} (\"{title}\"), made {when} "
    "from that conversation up to here. The conversation above is a copy of "
    "that session's, its record numbers kept, and session_search finds it "
    "there. From here on the two go their own ways. The workspace, its "
    "files and memory are shared with it; jobs and subagents started above "
    "belong to that session, and their notices go there.{scratch}{went_on}]"
)
# A subagent's fork (subagents/): ZERO's copy of its parent's finished turns,
# what differs said after the copied system message, its mode among it
SUBAGENT_FORKED = (
    "[ZERO started this session as a subagent of session {session} "
    "(\"{title}\"), {when}, from a copy of that session's conversation up to "
    "its last finished turn: the conversation above, its record numbers "
    "kept, which session_search finds there. From here on the two go their "
    "own ways. The workspace, its files and memory are shared with it; jobs "
    "and subagents started above belong to that session, and their notices "
    "go there.{mode}{scratch}]"
)
# In a subagent fork's note: its mode, and who answers its asks
SUBAGENT_MODE = " This session's {line}."
# In a fork's note, for a session with a scratch folder of its own
FORK_SCRATCH = " This session's scratch folder, $TMPDIR, is {path}, new and empty."
# In a fork's note, when the parent had gone on past the fork's point: its
# records from there (core/history.py's part_lines says what they did)
FORK_WENT_ON = (
    " That session had gone on past here when this fork was made: records "
    "{first}–{last} of its log, which session_search reads there. What was "
    "done there stays done: files and memory are as it left them.{part}"
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
        """A fork's first turn: what differs from the session it came from
        (FORKED; a subagent's, SUBAGENT_FORKED, with its mode). The scratch
        folder, said again when the context no longer
        names the one the session has: after a restart."""
        session = turn.session
        private_tmp = getattr(self.sandbox, "private_tmp", None)
        path = private_tmp() if private_tmp is not None else None
        head = session.log.first_record() or {}
        fork = head.get("forked_from")
        # Its own note: a fork of a fork copies its parent's, made earlier
        if fork and not any(r.get("kind") == "forked" and r["seq"] > fork["seq"]
                            for r in session.log.branch()):
            scratch = FORK_SCRATCH.format(path=path) if path else ""
            if fork.get("started_by") == "zero":
                text = SUBAGENT_FORKED.format(
                    session=fork["session"], title=fork["title"],
                    when=history.when(fork["time"]), scratch=scratch,
                    mode=SUBAGENT_MODE.format(line=self.mode) if self.mode else "",
                )
            else:
                went = fork.get("went_on")
                went_on = FORK_WENT_ON.format(**went) if went else ""
                text = FORKED.format(
                    session=fork["session"], title=fork["title"],
                    when=history.when(fork["time"]), scratch=scratch,
                    went_on=went_on,
                )
            session.write("note", kind="forked", text=text)
            return
        if path is None:
            return
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
