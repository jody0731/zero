"""The full screen's requests to ZERO's server, off its drawing thread (design
rule 12): one thread takes them in the order they were made, so a slow or
hung server leaves the screen drawing and its keys working, and what a
request returns goes back to the screen to be shown.
"""

import queue
import threading

# Requests that may wait at once; past it a new one is refused at once (a
# server that answers nothing must not pile up the user's every key)
WAITING_MAX = 32


class Requests:
    """`submit(work, then, failed, lane="normal")`: runs `work()` off the
    drawing thread, then hands `then(result)` or `failed(error)` to `deliver`.
    Normal requests run in FIFO order on one worker; `upload` and `urgent`
    (such as stop) run on dedicated lanes so slow uploads never block stop."""

    def __init__(self, deliver, waiting_max=WAITING_MAX):
        self.deliver = deliver
        self.waiting = queue.Queue(maxsize=waiting_max)
        self.lanes = {
            "normal": self.waiting,
            "upload": queue.Queue(maxsize=waiting_max),
            "urgent": queue.Queue(maxsize=waiting_max),
        }
        # Requests submitted and not yet answered, the one running included
        self.open = 0
        self.settled = threading.Condition()
        self.threads = {}
        for name, box in self.lanes.items():
            thread_name = "tui-requests" if name == "normal" else f"tui-{name}"
            thread = threading.Thread(
                target=self.run, args=(box,), name=thread_name, daemon=True,
            )
            thread.start()
            self.threads[name] = thread
        self.thread = self.threads["normal"]

    def submit(self, work, then=None, failed=None, *, lane="normal"):
        box = self.lanes.get(lane, self.waiting)
        try:
            with self.settled:
                self.open += 1
            box.put_nowait((work, then, failed))
        except queue.Full:
            self.done()
            error = TimeoutError(
                f"ZERO's server hasn't answered; {box.maxsize} "
                "requests wait, and this one is dropped"
            )
            if failed is not None:
                failed(error)

    def run(self, box=None):
        box = box or self.waiting
        while True:
            work, then, failed = box.get()
            if work is None:
                return
            try:
                result = work()
            except Exception as error:
                if failed is not None:
                    self.deliver(failed, error)
            else:
                if then is not None:
                    self.deliver(then, result)
            finally:
                self.done()

    def done(self):
        with self.settled:
            self.open -= 1
            self.settled.notify_all()

    def wait_answered(self, timeout=None):
        """True once every request made so far has its answer handed on."""
        with self.settled:
            return self.settled.wait_for(lambda: self.open == 0, timeout)

    def stop(self):
        """The threads end after the requests already waiting."""
        for box in self.lanes.values():
            try:
                box.put_nowait((None, None, None))
            except queue.Full:
                pass
