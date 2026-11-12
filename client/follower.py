"""Follow a session across server restarts, without depending on a terminal.

The server's `bye` says what to do when it closes the watch: "stopped" (by
the user) leaves the server down until the user sends something; "restart" (a
newer build) follows again at once. A watch that ends with no `bye` was a
crash: following again starts a server, which resumes the cut-off turn.
"""

import threading

from client.connection import NotRunning, ProtocolError


class Follower:
    def __init__(self, connection, state, renderer, after=0, emit=print,
                 on_event=None):
        """`on_event(event)`: told after each event is shown (the CLI marks
        what was seen and redraws)."""
        self.on_event = on_event
        self.connection = connection
        self.state = state
        self.renderer = renderer
        self.after = after
        self.emit = emit
        # True once the server was stopped by hand: following ended on purpose
        self.stopped = False
        self.leaving = threading.Event()
        self.lock = threading.Lock()
        self.stream = None

    def run(self):
        reconnecting = False
        while not self.leaving.is_set():
            try:
                stream = self.connection.watch(self.state.session, after=self.after)
            except (OSError, ValueError, NotRunning, ProtocolError) as error:
                self.failed(error)
                return
            with self.lock:
                active = not self.leaving.is_set()
                if active:
                    self.stream = stream
            if not active:
                stream.close()
                return
            try:
                for event in stream:
                    if self.leaving.is_set():
                        return
                    record = event.get("record")
                    if record is not None and record["seq"] <= self.after:
                        continue
                    self.state.event(event)
                    for line in self.renderer.event(event):
                        self.emit(line)
                    if record is not None:
                        self.after = record["seq"]
                        reconnecting = False
                    if self.on_event is not None:
                        self.on_event(event)
            except (OSError, NotRunning):
                pass
            except (ValueError, ProtocolError) as error:
                self.failed(error)
                return
            finally:
                self.close_stream()
            if stream.bye == "stopped":
                self.stopped = True
                if not self.leaving.is_set():
                    self.emit(
                        "(ZERO's server was stopped; it starts again when you "
                        "send something)"
                    )
                return
            if not self.leaving.is_set():
                if stream.bye == "behind":
                    # Followed again from the last record shown, unsaid
                    continue
                if not reconnecting:
                    if stream.bye == "restart":
                        self.emit("(the server restarts with newer code)")
                    else:
                        self.emit("(the server restarted; following again)")
                    reconnecting = True
                self.leaving.wait(0.2)

    def failed(self, error):
        if not self.leaving.is_set():
            self.emit(f"ZERO: could not follow {self.state.session}: {error}")

    def close_stream(self):
        with self.lock:
            stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def stop(self):
        self.leaving.set()
        self.close_stream()
