"""Who is on the other end of the socket: the user's own client, or one
of ZERO's own commands.

Any program running as the user can open the socket, and in auto+ (or a call
let out of the sandbox) that includes the commands ZERO runs: `zero -p yes
--session ID` answers another session's ask as the user's terminal would. ZERO's
own commands may look, and only the user's own client sends, answers, stops
or changes anything.

A peer is ZERO's own when its process descends from the host's, or, where
the host runs in its unit (systemd's transient `zero-host`, named to it in
ZERO_HOST_UNIT by client/launcher.py, and the host's cgroup is that unit's),
when it lives in the host's cgroup: a command whose parent is gone is taken
in by systemd but stays in the unit. What this leaves:
a command that starts `zero` in a unit of its own (`systemd-run --user
--scope zero …`), which the judge sees as it is. Sandboxed commands can't
reach the socket at all (sandbox/sandbox.py hides /run/user).
"""

import os
import socket
import struct

# Where client/launcher.py names the host's unit to it
UNIT_ENV = "ZERO_HOST_UNIT"
UNSET = object()


def refused(looking=()):
    """The no one of ZERO's own commands gets; `looking`: the parts' verbs
    that only look, named after the kernel's."""
    named = ", ".join(("list", "history", "watch", *looking))
    return (
        f"ZERO's own commands can look at sessions over this socket ({named}) "
        "but not send, answer, stop or change anything: that comes from the "
        "user's own client."
    )


def peer_pid(conn):
    """The process on the other end of a Unix socket, or None."""
    try:
        data = conn.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
    except OSError:
        return None
    pid, _, _ = struct.unpack("3i", data)
    return pid or None


def parent(pid):
    """A process's parent's pid, or None once it has gone."""
    try:
        text = open(f"/proc/{pid}/stat").read()
    except OSError:
        return None
    # The name in brackets may hold spaces; the state and the parent follow it
    fields = text.rpartition(")")[2].split()
    return int(fields[1]) if len(fields) > 1 else None


def cgroup(pid):
    try:
        return open(f"/proc/{pid}/cgroup").read()
    except OSError:
        return None


class Peers:
    def __init__(self, pid=None, unit=UNSET):
        """`unit`: the host's unit's name (client/launcher.py sets it), or None
        for none."""
        self.pid = pid or os.getpid()
        if unit is UNSET:
            unit = os.environ.get(UNIT_ENV)
        # Only the unit's own cgroup counts: a terminal's or a test's is
        # shared with the user's clients
        mine = cgroup(self.pid)
        in_unit = unit and mine and mine.rstrip().endswith(f"/{unit}.service")
        self.cgroup = mine if in_unit else None

    def own(self, pid):
        """Whether process `pid` is one of ZERO's own commands. The host's
        own process is not: only a test connects from it."""
        if pid is None or pid <= 1 or pid == self.pid:
            return False
        if self.cgroup is not None and cgroup(pid) == self.cgroup:
            return True
        seen = set()
        while pid is not None and pid > 1 and pid not in seen:
            seen.add(pid)
            pid = parent(pid)
            if pid == self.pid:
                return True
        return False
