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
    """`submit(work, then, failed)`: `work()` on the requests thread, then
    `then(result)` or `failed(error)` handed to `deliver`, which runs them on
    the screen."""

    def __init__(self, deliver, waiting_max=WAITING_MAX):
        self.deliver = deliver
        self.waiting = queue.Queue(maxsize=waiting_max)
        # Requests submitted and not yet answered, the one running included
        self.open = 0
        self.settled = threading.Condition()
        self.thread = threading.Thread(target=self.run, name="tui-requests",
                                       daemon=True)
        self.thread.start()

    def submit(self, work, then=None, failed=None):
        try:
            with self.settled:
                self.open += 1
            self.waiting.put_nowait((work, then, failed))
        except queue.Full:
            self.done()
            error = TimeoutError(
                f"ZERO's server hasn't answered; {self.waiting.maxsize} "
                "requests wait, and this one is dropped"
            )
            if failed is not None:
                failed(error)

    def run(self):
        while True:
            work, then, failed = self.waiting.get()
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
        """The thread ends after the requests already waiting."""
        try:
            self.waiting.put_nowait((None, None, None))
        except queue.Full:
            pass
