"""Where and when the agent is, so it doesn't spend steps finding out."""

import getpass
import platform
from datetime import datetime
from pathlib import Path

from core import entries, replay, tools
from core.hooks import Hook
from paths import changelog_version
from components.prompt.instructions import means_facts

# The version of ZERO this process runs, read once when it starts, as the
# server's build is (protocol/version.py)
ZERO_VERSION = changelog_version()
# A restart gives each session a new scratch folder (sandbox/sandbox.py's
# LASTS), while its system message keeps the one it was written with
NEW_SCRATCH = (
    "[Harness note: ZERO restarted, and this session's scratch folder, "
    "$TMPDIR, is now {path}, new and empty.]"
)
# A fork's first turn: whose fork it is and what differs from that session;
# its system message is the parent's byte for byte, so the cache holds
FORKED = (
    "[This session is a fork of session {session}{parent_title}, made {when} "
    "from that conversation up to here. The conversation above is a copy of "
    "that session's, its record numbers kept{finds}. From here on the two go "
    "their own ways. The workspace, its "
    "files and memory are shared with it; jobs and subagents started above "
    "belong to that session, and their notices go there.{scratch}{went_on}]"
)
# A subagent's fork (subagents/): ZERO's copy of its parent's finished turns,
# what differs said after the copied system message, its mode among it
SUBAGENT_FORKED = (
    "[ZERO started this session as a subagent of session {session} "
    "(\"{title}\"), {when}, from a copy of that session's conversation up to "
    "its last finished turn: the conversation above, its record numbers "
    "kept{which_finds}. From here on the two go their own ways. The "
    "workspace, its files and memory are shared with it; jobs "
    "and subagents started above belong to that session, and their notices "
    "go there.{mode}{scratch}]"
)
# In a subagent fork's note: its mode, and who answers its asks
SUBAGENT_MODE = " This session's {line}."
# In a fork's note, for a session with a scratch folder of its own
FORK_SCRATCH = " This session's scratch folder, $TMPDIR, is {path}, new and empty."
FORK_WENT_ON = " Later work in the parent may have changed the shared workspace."
# The tool that reads a log by its record numbers (tools declaring
# "reads_records" in their "prompt" metadata), in the notes above
FINDS = ", and {tool} finds it there"
WHICH_FINDS = ", which {tool} finds there"


def means_lines(context=None):
    """What each means of carrying out a task is for, a line a means, as the
    tools a session offers say it in their `prompt` metadata, in the order
    registered: only the means the session has."""
    context = context or {}
    lines = []
    for _, meta in tools.tools_with_metadata("prompt", context):
        line = meta.get("means")
        if line is None:
            continue
        if callable(line):
            line = line(context)
        if line:
            lines.append(line)
    return lines


def reader_clause(text, context):
    """`text` with the tool that reads a log by its record numbers, or
    nothing where the session offers none."""
    found = tools.tools_with_trait("reads_records", context)
    return text.format(tool=found[0][0]) if found else ""


class Means(Hook):
    """Whose pick the means of carrying out a task are, the user's trade
    between waiting and weighted tokens, and what each means is for
    (prompt/instructions.py, MEANS_FACTS): the first of the hooks' parts, so
    it stands under how a turn runs. `limits`: the session's settings, read
    when the system message is written. `weights`: what a token read from
    the cache and an output token count on the session's model, None for a
    model with no price."""

    def __init__(self, limits, weights=None):
        self.limits = limits
        self.weights = weights

    def system_prompt(self, session):
        return means_facts(means_lines(session.tool_context),
                           self.limits["time_or_tokens"], self.weights)


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
            f"{version_line()}"
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
        head = session.log.first_record()
        fork = head.entry.get("forked_from") if head is not None else None
        # Its own note: a fork of a fork copies its parent's, made earlier
        if fork and not any(r.seq > fork["seq"] and entries.has(r.entry, "fork")
                            for r in reversed(session.log.branch())):
            scratch = FORK_SCRATCH.format(path=path) if path else ""
            context = session.tool_context
            if fork.get("started_by") == "zero":
                text = SUBAGENT_FORKED.format(
                    session=fork["session"], title=fork["title"],
                    when=replay.when(fork["time"]), scratch=scratch,
                    mode=SUBAGENT_MODE.format(line=self.mode) if self.mode else "",
                    which_finds=reader_clause(WHICH_FINDS, context),
                )
            else:
                went = fork.get("went_on")
                went_on = FORK_WENT_ON + went.get("effects", "") if went else ""
                # A parent title can come from its first discarded request.
                parent_title = "" if went else f' ("{fork["title"]}")'
                text = FORKED.format(
                    session=fork["session"], parent_title=parent_title,
                    when=replay.when(fork["time"]), scratch=scratch,
                    went_on=went_on, finds=reader_clause(FINDS, context),
                )
            session.write("note", kind="forked", text=text)
            return
        if path is None:
            return
        if any(path in entries.project(entry) for entry in session.entries):
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


def version_line():
    """ZERO's version, for the environment; nothing when it has none."""
    return f"- ZERO's version: {ZERO_VERSION}\n" if ZERO_VERSION else ""


def os_name():
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return platform.system()
