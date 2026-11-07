"""What a component adds to the server: its service (jobs/service.py,
permissions/service.py).

The assembly makes the services and server/main.py hands them to the runtime, which
attaches each one, so the kernel names no component. A service may bring a table
in the one database, messages (described in protocol/messages.py; those
that only look named in `looking`), something to start and stop with the
server, what keeps the server up, a session's share of it, and the reply
step. Every hook does nothing until a service says otherwise, as core's
hooks do. Refused is the server's no, from the kernel or a service.
"""


class Refused(Exception):
    """A request the server won't take; the reason goes back to its sender."""


class Service:
    # The service's name: the runtime's `services` key
    name = ""
    # SQL creating its tables, run when the store opens
    schema = ""
    # Its tables with a `session` column: a deleted session's rows go too
    tables = ()
    # Its messages that only look, which ZERO's own commands may send too
    # (server/peers.py)
    looking_messages = ()

    def attach(self, runtime):
        """The runtime it is service of, once its store is open."""
        self.runtime = runtime

    def messages(self):
        """Its messages: name → handler(request), which returns the reply or
        raises the runtime's Refused."""
        return {}

    def begin(self):
        """At boot, before the repair of what a crash or a stop left."""

    def shutdown(self):
        """A clean stop, once running turns had their time."""

    def busy_with(self):
        """What of it keeps the server up: {reason: [ids]}, empty for nothing."""
        return {}

    def session_built(self, sid, session):
        """A harness the server built for this session."""

    def refuse_delete(self, sid):
        """Why the session can't be deleted now, or None."""
        return None

    def session_files(self, sid):
        """Its files of the session, to the trash with the session's log."""
        return []

    def annotate(self, rows):
        """Add its fields to the session list's rows."""

    def settled(self, sid):
        """What is left of it once nothing more happens on its own."""
        return {}

    def reply(self, sid, ask, payload):
        """The reply step, when a reply reaches a parked ask: (choice,
        verdict), or None to leave it to the box's choice alone."""
        return None
