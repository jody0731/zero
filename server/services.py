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


from dataclasses import dataclass, field


class Refused(Exception):
    """A request the server won't take; the reason goes back to its sender."""


@dataclass(frozen=True, slots=True)
class Principal:
    """Server-derived identity for one authenticated request."""

    pid: int | None = None
    scope: str | None = None
    relayed: bool = False
    command_session: str | None = None
    is_command: bool = False

    @property
    def owner_key(self):
        """Stable owner bucket for staged attachments and idempotency."""
        if self.scope is not None:
            return f"workflow:{self.scope}"
        if self.command_session is not None:
            return f"command:{self.command_session}"
        if self.is_command:
            return f"command_pid:{self.pid}"
        if self.relayed:
            return "relayed"
        return "local"

    @property
    def can_add_host_path(self):
        """Host-path attachment/add is refused when relayed, scoped, or a command."""
        return (
            not self.relayed
            and self.scope is None
            and self.command_session is None
            and not self.is_command
        )


@dataclass(slots=True)
class PreparedMessage:
    """Service-prepared additions to an admitted user message."""

    payload: dict = field(default_factory=dict)
    fallback_title: str | None = None
    has_content: bool = False
    claim: object = None
    after_commit: object = None


class Service:
    # The service's name: the runtime's `services` key
    name = ""
    # SQL creating its tables, run when the store opens
    schema = ""
    # Its tables' columns renamed since a database was made: table → {old
    # name: new name}, renamed in place when the store opens
    renamed_columns = {}
    # Its tables' columns added since a database was made: table → {name:
    # its SQL type}, added when the store opens
    added_columns = {}
    # Its tables with a `session` column: a deleted session's rows go too
    tables = ()
    # Optional capability flags advertised in `server/hello`'s `features`
    features = ()
    # Its messages that only look, which ZERO's own commands may send too
    # (server/peers.py)
    looking_messages = ()
    # Its messages whose handler receives `(request, principal)`
    principal_messages = ()
    # Its messages a workflow may send: one of ZERO's own commands, for the
    # session whose command it is, named in the message's `for_session`
    # (server/server.py checks the name against command_session)
    workflow_messages = ()
    # Fields of session/send it reads (the protocol declares them), handed on
    # to the user record of the turn the message opens
    message_fields = ()
    # Ask kinds that only accept replies through a component's private message:
    # kind → refusal text shown when a generic reply attempts to answer it
    private_ask_kinds = {}

    def attach(self, runtime):
        """The runtime it is service of, once its store is open."""
        self.runtime = runtime

    def redact(self, value):
        """Transform data a tool produced before it is persisted (a job's
        command)."""
        return value

    def redact_entry(self, entry):
        """Transform what a tool wrote into a session log before it is
        persisted (sessions/log.py, tool_output)."""
        return entry

    def stream_filter(self):
        """Factory returning a streaming byte filter `(data, final=False) -> bytes`,
        or None when this service adds no stream filter."""
        return None

    def messages(self):
        """Its messages: name → handler(request), which returns the reply or
        raises the runtime's Refused."""
        return {}

    def streaming_messages(self):
        """Its streaming watch messages: name → handler(conn, request, protocol,
        principal, send_event)."""
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

    def turn_ended(self, sid, outcome):
        """A turn of the session ended (its turn_end written) or parked on
        an ask (outcome "waiting"), its row settled."""

    def command_session(self, unix_session):
        """The session whose command leads the Unix session `unix_session`
        (a process's session id, as /proc gives it), if this service started
        that command; else None."""
        return None

    def permission_records(self, sid):
        """Additional consent records belonging to this session."""
        return []

    def unattended(self, sid):
        """Whether this session is already doing handed-off work."""
        return False

    def refuse_delete(self, sid):
        """Why the session can't be deleted now, or None."""
        return None

    def fork_effects(self, sid, left):
        """Concrete effects of the parent's later work when a user forks earlier."""
        return ""

    def refuse_rewind(self, sid):
        """Why the session's conversation can't be rewound now, or None."""
        return None

    def session_files(self, sid):
        """Its files of the session, to the trash with the session's log."""
        return []

    def session_deleted(self, sid):
        """The session was deleted: what it kept of it, let go of."""

    def meter_line(self):
        """The line the user's context meter shows a session's context kept
        under (the context component's setting), or None to say nothing."""
        return None

    def annotate(self, rows):
        """Add its fields to the session list's rows."""

    def settled(self, sid):
        """What is left of it once nothing more happens on its own."""
        return {}

    def rebuild(self, db, sid, records):
        """Its rows of the session as the session's log says (design rule
        9), inside the store's transaction `db`: called at start for a
        session whose log is ahead of the database. `records`: the whole
        log, in the current format."""

    def reply(self, sid, ask, payload):
        """The reply step, when a reply reaches a parked ask: (choice,
        verdict), or None to leave it to the box's choice alone."""
        return None

    def prepare_message(self, message, extra, *, principal=None):
        """Validate and prepare extra message content before inbox admission."""
        return None

    def rewrite_fork_records(self, parent_sid, child_sid, records):
        """Rewrite copied log records for `child_sid` before writing a fork log."""
        return records

    def session_forked(self, db, parent_sid, child_sid, copied_records):
        """Update service rows inside `add_session` for a newly forked session."""

    def after_fork(self, child_sid, copied_records):
        """Post-commit work after a fork is saved."""

    def before_trash(self, sid):
        """Preserve any service references before a session's files move to trash."""

    def message_label(self, entry):
        """Fallback display label for a user message with blank text, or None."""
        return None

    def message_restore(self, entry):
        """Extra fields restored on rewind/fork/user_messages, or None."""
        return None
