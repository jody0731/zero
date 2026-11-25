"""Session subscriptions, reply drafts, and list notifications."""

import threading
from collections import deque

from server.services import Refused

# A watcher further behind reconnects and reads the log for catch-up.
WATCH_QUEUE_MAX = 2000
# Bounded diff ring buffer for incremental session/list watches (design rule 12).
LIST_DIFF_MAX = 2000
BEHIND = "behind"
DELETED = "deleted"


class Watcher:
    """One watching connection's queue: records and events, in order, at
    most `most` of them (design rule 12: every queue has a cap)."""

    def __init__(self, most=None):
        self.events = deque()
        self.most = WATCH_QUEUE_MAX if most is None else most
        self.ready = threading.Condition()
        # Why the server closes this watch: a `bye` reason, once it does
        self.bye = None

    def put(self, event):
        with self.ready:
            if self.bye is not None:
                return
            # A draft holds the whole reply so far, so a newer one replaces
            # one still queued: a slow reader gets the latest, not each
            if "draft" in event and self.events and "draft" in self.events[-1]:
                self.events[-1] = event
            elif len(self.events) >= self.most:
                # Too far behind: let go, to follow again from the log
                self.events.clear()
                self.bye = BEHIND
            else:
                self.events.append(event)
            self.ready.notify_all()

    def close(self, reason):
        with self.ready:
            self.bye = self.bye or reason
            self.ready.notify_all()

    def take(self, timeout):
        """What came since the last take, waiting up to `timeout` for any."""
        with self.ready:
            self.ready.wait_for(lambda: self.events or self.bye, timeout)
            events = list(self.events)
            self.events.clear()
            return events


class WatcherHub:
    def __init__(self, runtime=None, *, list_diff_max=LIST_DIFF_MAX):
        self.runtime = runtime
        self.list_version = 0
        self.list_changed = threading.Condition()
        self.list_diffs = deque(maxlen=list_diff_max)

    def notify_list_changed(self, changed_sids=()):
        if not changed_sids:
            return
        touched = frozenset(changed_sids)
        with self.list_changed:
            self.list_version += 1
            self.list_diffs.append((self.list_version, touched))
            self.list_changed.notify_all()

    def list_changes_since(self, since_version):
        """Return `(current_version, touched_sids_or_None)`.

        `touched` is `None` when `since_version` fell past the bounded diff
        ring buffer (`LIST_DIFF_MAX`) and the caller must resync in full.
        """
        with self.list_changed:
            current = self.list_version
            if current <= since_version:
                return current, frozenset()
            if not self.list_diffs:
                return current, None
            oldest_version = self.list_diffs[0][0]
            if since_version < oldest_version - 1:
                return current, None
            touched = set()
            for ver, sids in self.list_diffs:
                if ver > since_version:
                    touched.update(sids)
            return current, frozenset(touched)

    def wait_list(self, version, timeout):
        """The list's version once it passes `version`, or after `timeout`."""
        with self.list_changed:
            self.list_changed.wait_for(
                lambda: self.list_version != version, timeout
            )
            return self.list_version

    def meter(self, sid, log=None):
        """Publish changes to the current branch's cached context meter."""
        runtime = self.runtime
        state = runtime.state(sid)
        before = state.meter
        now = runtime.context_meter(sid, log=log)
        state.meter = now
        if now is not None and now != before:
            runtime.publish(sid, {"context": now})

    def draft(self, sid, step, draft):
        """The model's reply as it streams in (core/llm.py, Draft), to the
        session's watchers: the text so far, how much reasoning, the tools
        whose calls have begun. Kept until the next record, for a watch that
        starts mid-reply; never logged."""
        runtime = self.runtime
        event = {"draft": {
            "step": step,
            "text": draft.content,
            "reasoning": len(draft.reasoning),
            "calls": [call["name"] for _, call in sorted(draft.calls.items())],
        }}
        runtime.state(sid).draft = event
        runtime.publish(sid, event)

    def current_draft(self, sid):
        """The draft event of the reply streaming in now, or None."""
        runtime = self.runtime
        state = runtime.states.get(sid)
        return None if state is None else state.draft

    def subscribe(self, sid):
        """A watcher for the session's records from now on; subscribe before
        reading the log's catch-up, so nothing falls between the two."""
        runtime = self.runtime
        if runtime.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        token, watcher = object(), Watcher()
        state = runtime.state(sid)
        with state.lock:
            state.watchers = {**state.watchers, token: watcher}
        return token, watcher

    def unsubscribe(self, sid, token):
        runtime = self.runtime
        state = runtime.states.get(sid)
        if state is None:
            return
        with state.lock:
            state.watchers = {k: w for k, w in state.watchers.items() if k is not token}

    def publish(self, sid, event):
        runtime = self.runtime
        state = runtime.states.get(sid)
        if state is None:
            return
        for watcher in state.watchers.values():
            watcher.put(event)

    def close_watchers(self, reason):
        """Every watch ends with a `bye` and its reason."""
        runtime = self.runtime
        for _, state in runtime.all_states():
            for watcher in state.watchers.values():
                watcher.close(reason)

    def forget(self, sid):
        """A deleted session's state let go of: its watches end with a `bye`
        "deleted", its harness and draft go with it."""
        runtime = self.runtime
        with runtime.door:
            state = runtime.states.pop(sid, None)
        if state is None:
            return
        for watcher in state.watchers.values():
            watcher.close(DELETED)
