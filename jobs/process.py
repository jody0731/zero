"""A job's process: started from one long-lived thread, in its own process
group, its output pumped into files, stopped by group.

The shell runs its foreground commands with the same pump and stop
(shell/), and clips their output as a job's look does.
"""

import os
import secrets
import selectors
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.control import Stopped

# The start and the end of a job's output are kept; the middle is dropped
HEAD_BYTES = 8 << 20
TAIL_BYTES = 8 << 20
# After SIGTERM, how long a command gets to clean up before SIGKILL
KILL_GRACE_SECONDS = 1.0
DRAIN_SECONDS = 2.0


def new_id():
    return f"job-{secrets.token_hex(3)}"


class Spawner:
    """Starts processes from one long-lived thread.

    bwrap's --die-with-parent kills a command when the thread that started it
    exits (PR_SET_PDEATHSIG follows the creating thread), and a session's runner
    thread exits after each turn; a job must outlive it.
    """

    def __init__(self):
        # One worker, and an idle worker never exits
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="spawner")

    def popen(self, argv, **options):
        return self.pool.submit(subprocess.Popen, argv, **options).result()


SPAWNER = Spawner()


class OutputFiles:
    """A job's output on disk: the first `head` bytes, then the last `tail` to
    2 × `tail` bytes in rotating files named by where they start. What falls
    between is counted, never kept, so a runaway printer can't fill the disk."""

    def __init__(self, base, head=HEAD_BYTES, tail=TAIL_BYTES):
        self.base = Path(base)
        self.head, self.tail = head, tail
        self.base.parent.mkdir(parents=True, exist_ok=True)
        self.start_file = self.base.with_suffix(".out")
        self.start_file.touch()
        tails = self.tails()
        self.current = tails[-1] if tails else None
        if self.current is None:
            self.size = self.start_file.stat().st_size
        else:
            self.size = self.offset(self.current) + self.current.stat().st_size

    def tails(self):
        found = self.base.parent.glob(f"{self.base.name}.tail-*")
        return sorted(found, key=self.offset)

    @staticmethod
    def offset(path):
        return int(path.name.rsplit("-", 1)[1])

    def add(self, data, final=False):
        """The pump's sink (read_output); `final` changes nothing."""
        if not data:
            return
        room = self.head - self.size
        if room > 0:
            with open(self.start_file, "ab") as out:
                out.write(data[:room])
            self.size += min(room, len(data))
            data = data[room:]
        while data:
            if self.current is None or self.current.stat().st_size >= self.tail:
                self.current = self.base.parent / f"{self.base.name}.tail-{self.size}"
                tails = self.tails()
                # Keep the newest full tail file and the one just begun
                for old in tails[:-1]:
                    old.unlink()
                self.current.touch()
            take = self.tail - self.current.stat().st_size
            with open(self.current, "ab") as out:
                out.write(data[:take])
            self.size += min(take, len(data))
            data = data[take:]

    def read(self, start):
        """The kept output from byte `start` on, and how much of it was dropped."""
        text, dropped, _ = self.read_through(start)
        return text, dropped

    def read_through(self, start):
        """As `read`, with the byte it read up to: the pump may write on
        while this reads, past the size this was made with, and the next read
        starts where this one ended, not where the size said."""
        pieces = [(0, self.start_file.read_bytes())]
        pieces += [(self.offset(p), p.read_bytes()) for p in self.tails()]
        out = bytearray()
        position = start
        dropped = 0
        for offset, data in pieces:
            end = offset + len(data)
            if end <= position:
                continue
            if offset > position:
                dropped += offset - position
                position = offset
            out += data[position - offset:]
            position = end
        dropped += max(0, self.size - position)
        return out.decode("utf-8", errors="replace"), dropped, max(position,
                                                                   start)


class Job:
    """One process and the thread that pumps its output and waits for its end.

    `on_end(job, code)` runs in that thread once the process has ended and its
    output is on disk. `stopped_by` says who stopped it, if anyone did."""

    def __init__(self, id, proc, files, on_end):
        self.id = id
        self.proc = proc
        self.files = files
        self.on_end = on_end
        self.stop_flag = threading.Event()
        self.stopped_by = None
        self.thread = threading.Thread(target=self.run, name=id, daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        try:
            read_output(self.proc, self.files, stop=self.stop_flag)
        except Stopped:
            stop_group(self.proc)
            drain(self.proc, self.files)
        finally:
            self.proc.stdout.close()
        code = self.proc.wait()
        self.on_end(self, code)

    def stop(self, by):
        """Ask the pump to end the process group; True if it was still running."""
        if self.stop_flag.is_set() or self.proc.poll() is not None:
            return False
        self.stopped_by = by
        self.stop_flag.set()
        return True


def read_output(proc, output, timeout=None, stop=None):
    """Drain stdout in chunks, including pipes held open by child processes."""
    deadline = None if timeout is None else time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        while True:
            if stop is not None and stop.is_set():
                raise Stopped()
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout)
            interval = 0.2 if remaining is None else min(0.2, remaining)
            if not selector.select(interval):
                continue
            data = os.read(proc.stdout.fileno(), 65536)
            if not data:
                break
            output.add(data)
    output.add(b"", final=True)
    while proc.poll() is None:
        if stop is not None and stop.is_set():
            raise Stopped()
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        try:
            proc.wait(timeout=0.2 if remaining is None else min(0.2, remaining))
        except subprocess.TimeoutExpired:
            continue


def drain(proc, output):
    """True when a pipe held outside the killed group had to be cut off."""
    try:
        read_output(proc, output, timeout=DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        output.add(b"", final=True)
        return True
    return False


def stop_group(proc):
    """End a command's whole process group: SIGTERM, a short grace, then SIGKILL.

    The grace lets commands clean up (temp files, locks), as Codex and DSH do.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    # Children may outlive bash itself, so kill whatever is left in the group
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def clip(text, limit):
    """Keep the start and the end of long output, cut the middle."""
    if len(text) <= limit:
        return text
    half = limit // 2
    cut = len(text) - limit
    return f"{text[:half]}\n[... {cut} characters cut ...]\n{text[-half:]}"
