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


DIGEST_VERSION = "v2:"


def legacy_request_hash(request):
    """Pre-principal unversioned request-only digest (32 hex chars)."""
    asked = {k: v for k, v in request.items() if k not in NOT_HASHED}
    text = json.dumps(asked, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def is_legacy_digest(digest):
    """True when `digest` is a pre-upgrade unversioned request-only hash."""
    return isinstance(digest, str) and not digest.startswith(DIGEST_VERSION)


def matches_request_hash(stored, digest, *, legacy_digest=None, allow_legacy=False):
    """Compare stored idempotency digest against current (and authorized legacy)."""
    if stored == digest:
        return True
    if not is_legacy_digest(stored):
        return False
    allowed = allow_legacy() if callable(allow_legacy) else allow_legacy
    if not allowed:
        return False
    expected = legacy_digest() if callable(legacy_digest) else legacy_digest
    return expected is not None and stored == expected


def request_hash(request, *, principal=None):
    """What a request asks, as a versioned digest scoped to its principal."""
    asked = {k: v for k, v in request.items() if k not in NOT_HASHED}
    if principal is None:
        return legacy_request_hash(asked)
    target = {"principal": principal.owner_key, "request": asked}
    text = json.dumps(target, sort_keys=True, ensure_ascii=False, default=str)
    return f"{DIGEST_VERSION}{hashlib.sha256(text.encode()).hexdigest()[:32]}"


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

    def run(self, name, request, answer, *, principal=None, allow_legacy=True,
            legacy_check=None):
        """`answer(request)`'s reply, run once for the request's id; the
        first reply again for an id already run."""
        msg_id = request["id"]
        full_request = {**request, "message": name}
        digest = request_hash(full_request, principal=principal)
        with self.lock:
            waiting = self.running.get(msg_id)
            if waiting is None:
                mine = threading.Event()
                self.running[msg_id] = mine
        if waiting is not None:
            if not waiting.wait(self.wait_seconds):
                return error(STILL_RUNNING.format(id=msg_id,
                                                  seconds=self.wait_seconds))
            return self._replay_checked(
                msg_id,
                self.store.message_reply(msg_id),
                digest,
                full_request,
                name,
                request,
                principal,
                allow_legacy,
                legacy_check,
            )
        try:
            found = self.store.message_reply(msg_id)
            if found is not None:
                return self._replay_checked(
                    msg_id,
                    found,
                    digest,
                    full_request,
                    name,
                    request,
                    principal,
                    allow_legacy,
                    legacy_check,
                )
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

    @classmethod
    def _replay_checked(cls, msg_id, found, digest, full_request, name,
                        request, principal, allow_legacy, legacy_check):
        if found is None or found["request_hash"] == digest:
            return cls.replay(msg_id, found, digest)
        allowed = (
            legacy_check(name, request, principal)
            if legacy_check is not None else allow_legacy
        )
        legacy_digest = (
            legacy_request_hash(full_request)
            if allowed and principal is not None
            and is_legacy_digest(found["request_hash"]) else None
        )
        return cls.replay(
            msg_id,
            found,
            digest,
            legacy_digest=legacy_digest,
            allow_legacy=allowed,
        )

    @staticmethod
    def replay(msg_id, found, digest, *, legacy_digest=None, allow_legacy=False):
        if found is None:
            return error(CUT_OFF.format(id=msg_id))
        if not matches_request_hash(
            found["request_hash"],
            digest,
            legacy_digest=legacy_digest,
            allow_legacy=allow_legacy,
        ):
            return error(OTHER_REQUEST.format(id=msg_id))
        if found["state"] != "done":
            return error(CUT_OFF.format(id=msg_id))
        return {**json.loads(found["reply"]), "replayed": True}


def error(text):
    return {"ok": False, "error": text}
