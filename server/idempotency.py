"""Each message that changes something runs once by its id (design rule 6:
safe to send twice).

The sender picks the id, new for each message, and sends the same id again
when it doesn't know whether the first got through (a dropped connection, a
server that stopped as it answered). The id is claimed in the database
before the message runs (the message_replies table), and its reply kept
there once it has; the same id again gets that reply, marked `replayed`, and
runs nothing (Stripe's idempotency keys, kept as Hermes keeps its runs).
The same id with another request is refused; a copy that comes while the
first runs waits for its reply; an id whose run a stop cut off before its
reply was kept is answered so, since whether it ran is unknown: so a
message runs at most once. Replies are kept a day (KEEP_SECONDS), far past
any retry.

The inbox's own messages (session/send, session/continue, session/stop) keep
their id in their inbox row instead, which commits with the change itself
(server/runtime.py).
"""

import hashlib
import json
import threading
import time

# How long an id's reply is kept, and how often the old ones are swept
KEEP_SECONDS = 24 * 3600
SWEEP_EVERY_SECONDS = 3600
# How long a copy waits for the first's reply: the longest a message runs
# (a model server's connect test, a model's features measured)
WAIT_SECONDS = 600
# What a request's hash leaves out: how it was sent, not what it asks
NOT_HASHED = ("id", "protocol", "token")

OTHER_REQUEST = ("message id {id!r} was used for another request; a new "
                 "message takes a new id")
CUT_OFF = ("message id {id!r} was cut off by the server's stop before its "
           "reply, so whether it ran is unknown: look before sending it "
           "again with a new id")
STILL_RUNNING = ("message id {id!r} is still running after {seconds} s; "
                 "send it again later for its reply")


def request_hash(request):
    """What a request asks, as a short hash: the same id must ask the same."""
    asked = {k: v for k, v in request.items() if k not in NOT_HASHED}
    text = json.dumps(asked, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:32]


class MessageReplies:
    def __init__(self, store, wait_seconds=WAIT_SECONDS):
        self.store = store
        self.wait_seconds = wait_seconds
        # The ids running now, each with what its copies wait on
        self.running = {}
        self.lock = threading.Lock()
        self.swept_at = 0.0

    def start(self):
        """At the server's start: what a stop cut off is said so, and the
        old replies swept."""
        self.store.cut_off_replies()
        self.sweep()

    def sweep(self, now=None):
        now = time.time() if now is None else now
        self.swept_at = now
        self.store.forget_replies(now - KEEP_SECONDS)

    def run(self, name, request, answer):
        """`answer(request)`'s reply, run once for the request's id; the
        first reply again for an id already run."""
        msg_id = request["id"]
        digest = request_hash({**request, "message": name})
        with self.lock:
            waiting = self.running.get(msg_id)
            if waiting is None:
                mine = threading.Event()
                self.running[msg_id] = mine
        if waiting is not None:
            if not waiting.wait(self.wait_seconds):
                return error(STILL_RUNNING.format(id=msg_id,
                                                  seconds=self.wait_seconds))
            return self.replay(msg_id, self.store.message_reply(msg_id), digest)
        try:
            found = self.store.message_reply(msg_id)
            if found is not None:
                return self.replay(msg_id, found, digest)
            if time.time() - self.swept_at > SWEEP_EVERY_SECONDS:
                self.sweep()
            self.store.claim_reply(msg_id, name, digest)
            reply = answer(request)
            self.store.keep_reply(msg_id, json.dumps(reply, ensure_ascii=False))
            return reply
        finally:
            with self.lock:
                self.running.pop(msg_id, None)
            mine.set()

    @staticmethod
    def replay(msg_id, found, digest):
        if found is None:
            return error(CUT_OFF.format(id=msg_id))
        if found["request_hash"] != digest:
            return error(OTHER_REQUEST.format(id=msg_id))
        if found["state"] != "done":
            return error(CUT_OFF.format(id=msg_id))
        return {**json.loads(found["reply"]), "replayed": True}


def error(text):
    return {"ok": False, "error": text}
