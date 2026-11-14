"""Who is on the other end of a loopback TCP connection, as the kernel says:
the user its socket belongs to and the process holding it.

A Unix socket names its peer (SO_PEERCRED); a TCP connection doesn't, but on
Linux the kernel's table of TCP sockets (/proc/net/tcp, tcp6) lists each with
its owner's uid and its inode, and the process holding that inode has it
among its open files (/proc/<pid>/fd), which is how `ss -p` finds it. Both
ends of a loopback connection are in the table: the client's socket is the
one whose local address is the connection's far end. The scan reads only
the user's own processes (others' fd folders can't be read) and takes about
16 ms over a desktop's five hundred processes.

Elsewhere (no /proc: another system, until 7.10's doors) the kernel says
nothing here and the door asks for the token instead.
"""

import os
import socket
import struct
from dataclasses import dataclass
from pathlib import Path

PROC = Path("/proc")
# The kernel's TCP tables, by address family
TABLES = {socket.AF_INET: "net/tcp", socket.AF_INET6: "net/tcp6"}


@dataclass(frozen=True)
class Peer:
    """A connection's client: its user, and its process where one of the
    user's processes holds the socket (None: not found)."""

    uid: int
    pid: int | None


def kernel_address(host, port, family):
    """An address as the kernel's table prints it: the address's 32-bit
    words in the machine's byte order, in hex, then the port."""
    packed = socket.inet_pton(family, host)
    words = struct.unpack(f"={len(packed) // 4}I", packed)
    return "".join(f"{word:08X}" for word in words) + f":{port:04X}"


def client_socket(client, server, family, proc=PROC):
    """(uid, inode) of the socket at `client` connected to `server`, each a
    (host, port), from the kernel's table; None where it isn't there."""
    table = proc / TABLES[family]
    local = kernel_address(*client[:2], family)
    remote = kernel_address(*server[:2], family)
    try:
        lines = table.read_text().splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        # sl, local, remote, st, tx:rx, tr:when, retrnsmt, uid, timeout, inode
        if len(fields) > 9 and fields[1] == local and fields[2] == remote:
            return int(fields[7]), int(fields[9])
    return None


def holder(inode, proc=PROC):
    """The process holding socket `inode` among those whose open files this
    user may read, or None."""
    target = f"socket:[{inode}]"
    try:
        pids = [name for name in os.listdir(proc) if name.isdigit()]
    except OSError:
        return None
    for pid in pids:
        folder = proc / pid / "fd"
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            try:
                if os.readlink(folder / name) == target:
                    return int(pid)
            except OSError:
                continue
    return None


def peer_of(conn, proc=PROC):
    """The Peer on the other end of an accepted TCP connection, or None where
    the kernel's table doesn't say (no /proc, another network namespace)."""
    family = conn.family
    if family not in TABLES:
        return None
    try:
        client = conn.getpeername()
        server = conn.getsockname()
    except OSError:
        return None
    found = client_socket(client, server, family, proc)
    if found is None:
        return None
    uid, inode = found
    if uid != os.getuid():
        return Peer(uid, None)
    return Peer(uid, holder(inode, proc))
