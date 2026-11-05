"""Optional turn controls supplied by a host, independent of how it stores work."""

import threading


class Stopped(KeyboardInterrupt):
    def __init__(self, detail=None):
        self.detail = detail
        super().__init__(detail)


class Control:
    """`steers()` and `delivered(steer)`: messages that join the turn. `draft`
    (step, draft): the model's reply as it streams in (core/llm.py, Draft),
    for the host's watchers; never logged."""

    def __init__(self, steers=None, delivered=None, draft=None):
        self.stop = threading.Event()
        self.stop_detail = "stop"
        self.steers = steers if steers is not None else lambda: []
        self.delivered = delivered if delivered is not None else lambda steer: None
        self.draft = draft if draft is not None else lambda step, draft: None

    def check(self):
        if self.stop.is_set():
            raise Stopped(self.stop_detail)
