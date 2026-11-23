"""Plain text from Protocol 8 log records and ask events."""

from datetime import datetime

from protocol import entries


def duration(record, started=None, restarts=0):
    if started and restarts:
        seconds = max(0, int((
            datetime.fromisoformat(record["time"]) - datetime.fromisoformat(started)
        ).total_seconds()))
        count = "once" if restarts == 1 else f"{restarts} times"
        return f"{seconds} s in all, resumed {count}"
    fields = entries.fields_of(record) if "entry" in record else record
    return f"{fields.get('seconds', 0)} s"


def short_tokens(count):
    """A count of tokens as the meter shows it: 950, 42k, 1M, 1.5M."""
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.0f}k"
    return f"{count / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"


def meter_text(context):
    """The context's meter in a few words, `context 42k/1M · cache 96%`; ""
    with nothing to show."""
    if not context or not context.get("tokens"):
        return ""
    text = f"context {short_tokens(context['tokens'])}"
    if context.get("line"):
        text += f"/{short_tokens(context['line'])}"
    if context.get("cached_share") is not None:
        text += f" · cache {context['cached_share']:.0%}"
    return text


# Who or what started a compaction, as a transcript says it
COMPACTED_BY = {"line": "at its line", "zero": "asked by ZERO",
                "user": "asked by you", "refused": "after the server refused "
                "a request as too long"}


# The entry types a compaction leaves: one, a failed one, or one with nothing
# older than the newest step to hand off
COMPACTION_RECORDS = entries.COMPACTION_TYPES


def compaction_text(record):
    """A compaction's record in a line: why, and the tokens before and
    after."""
    kind = entries.entry_type(record)
    fields = entries.fields_of(record)
    why = COMPACTED_BY.get(fields.get("reason"), fields.get("reason"))
    if kind == "context/compaction_failed":
        return f"compaction {why} failed: {fields.get('error')}"
    if kind == "context/nothing_to_compact":
        return (f"compaction {why}: nothing to hand off, as the context holds "
                "only its newest step")
    before = short_tokens(fields.get("tokens_before") or 0)
    after = short_tokens(fields.get("tokens_after") or 0)
    how = "handed off" if entries.compaction_summary(record) else "old outputs cleared"
    text = f"context compacted {why}, {how}: {before} → {after} tokens"
    # What its askers said the handoff keeps, in a line
    words = " ".join(" ".join((fields.get("instructions") or {}).values()).split())
    if words:
        text += f', keeping "{clipped_line(words)}"'
    return text


def clipped_line(text, chars=80):
    """`text` cut to `chars` characters, an ellipsis where it was cut."""
    return text if len(text) <= chars else text[:chars - 1].rstrip() + "…"


def model_failure_heading(failure):
    return f"Model request failed · {failure['server_title']}"


def model_failure_details(failure):
    operation = failure["operation"]
    operations = {"generate_reply": "Generate reply", "test_model": "Test model",
                  "compact_context": "Compact context"}
    lines = [operations.get(operation, operation)]
    if failure.get("http_status") is not None:
        lines.append(f"HTTP {failure['http_status']}")
    lines.extend(failure[field]
                 for field in ("provider_code", "provider_message", "observation")
                 if failure.get(field))
    return "\n".join(lines)


def rewind_text(record):
    """A rewind, in a line: the records it left behind."""
    fields = entries.fields_of(record)
    return (f"rewound here: records {fields['left_from']}–{fields['left_to']} "
            "left behind, kept in the log; files stay as they are")


# What a client says to `session/compact`'s reply
COMPACTING = {"now": "Compacting now.",
              "before_next_step": "Compacting before the turn's next step."}


def clipped(text):
    lines = text.splitlines()
    if len(lines) <= 10:
        return lines
    return lines[:5] + [f"[... {len(lines) - 10} lines cut ...]"] + lines[-5:]


def outcome_of(record):
    """A tool record's outcome as (ok, exit code or None, wall time, output
    lines), for the command blocks: from the data the record's entry carries
    beside its text (whether the call ran and failed, its tool's fields, where
    its output starts in the text), never from its words."""
    data = entries.fields_of(record)
    result = data.get("result") or ""
    fields = data.get("fields") or {}
    output_at = data.get("output_at")
    output = result[output_at:] if output_at is not None else result
    seconds = fields.get("wall_seconds")
    took = f"{seconds:.1f} s" if isinstance(seconds, (int, float)) else ""
    ok = bool(data.get("ran")) and not data.get("failed")
    return ok, fields.get("exit_code"), took, output.splitlines()


def who(source, mine=None):
    """Whose message: yours (from this terminal or another, with no mark for
    that), or a job's."""
    if source in (None, mine, "cli") or source.startswith("terminal-"):
        return "you"
    return source


def interaction_label(record):
    """Human-readable actor/action/target label for a wire record or search hit."""
    if "entry" in record:
        event = entries.interaction(record) or {}
        source = entries.field(record, "source")
    else:
        event = record.get("interaction") or {}
        source = record.get("source")
    sender = record.get("sender_name") or who(event.get("sender") or source)
    verbs = {"approve": "approved", "decline": "declined", "answer": "answered",
             "ask": "asked", "queue": "queued", "steer": "steer"}
    action = verbs.get(event.get("action"), "")
    ask = event.get("target", {}).get("ask", "")
    return " ".join(word for word in (sender, action, ask) if word)


def divider(title, workspace, width=80):
    text = f"── {title} · {workspace} "
    return text + "─" * max(3, width - len(text))


def jobs_text(jobs, sessions=False):
    """The server's job rows, each with its state in words (`state_text`)."""
    return "\n".join(
        f"  {row['id']}  {row['session'] + '  ' if sessions else ''}"
        f"{row['state_text']}  {row['command']}"
        for row in jobs
    )


def ask_line(ask):
    """An ask on one line, for plain output: its command and the reason the
    user reads (from either a `sessions/ask` wire record or a `row.ask`)."""
    data = entries.fields_of(ask) if "entry" in ask else ask
    if data.get("kind") == "secret":
        title = data.get("about") or "Private information needed"
        return title + ": use the masked form or zero private-input SESSION."
    if data.get("kind") == "question":
        return f"Question: {data['command']} (waiting for an answer)"
    reason = data.get("reason") or ""
    return f"? {data['tool']}: {data['command']}" + (f" ({reason})" if reason else "")


def call_line(call, summaries=None):
    """A call in a line, as the transcript shows it: the server's summary,
    which names the call (`read notes.txt`, `web_fetch URL`), a command's
    after a prompt's `$`."""
    line = command_of(call, summaries)
    if entries.call_name(call) == "bash":
        return f"$ {line}"
    return line


def command_of(call, summaries=None):
    """A call as a line, as an ask shows it: the server's summary of it (an
    assistant record's `call_summaries`, by the call's id), else its raw
    arguments."""
    found = (summaries or {}).get(entries.call_id(call))
    if found is not None:
        return found
    return entries.call_arguments(call)


def grants_text(grants):
    """/permissions' list: numbered, who granted it and for how long."""
    if not grants:
        return "Nothing runs without asking here, beyond an approved plan's calls."
    lines = []
    for n, grant in enumerate(grants, 1):
        who = "the judge" if grant["by"] == "judge" else "you"
        how = "in every session" if grant["scope"] == "saved" else (
            "in this session")
        lines.append(f"{n}. {grant['description']}: allowed by {who}, {how}")
    lines.append("\n/permissions forget N takes one back.")
    return "\n".join(lines)


# The judge's settings, as a switch line names them
SWITCH_NAMES = {"judge_model": "judge", "judge_thinking_effort": "judge's thinking"}


def switch_text(record):
    """A switch as the transcript shows it: "mode: default → auto"; the
    judge's too, which the model doesn't read."""
    fields = entries.fields_of(record)
    before = fields.get("from") or {}

    def said(name, value):
        # The judge's model, unset, is the session's own
        if name == "judge_model" and not value:
            return "the session's model"
        return value

    return " · ".join(
        f"{SWITCH_NAMES.get(name, name)}: {said(name, before.get(name, '?'))} "
        f"→ {said(name, value)}"
        for name, value in (fields.get("to") or {}).items()
    )


class Renderer:
    def __init__(self, source=None):
        self.source = source
        self.started = None
        self.restarts = 0

    def event(self, event):
        record = event.get("record") or {}
        kind = entries.entry_type(record)
        fields = entries.fields_of(record)
        if record.get("audience") == "model":
            self.restarts += fields.get("kind") == "restarted"
            return []
        if entries.is_turn_input(record):
            if not fields.get("steer"):
                self.started, self.restarts = record.get("time"), 0
            if entries.is_continue(record):
                return ["(you asked ZERO to go on)"]
            source = fields.get("source")
            # This terminal echoed its own steers and replies as they were sent
            if self.source is not None and source == self.source and fields.get(
                "steer"
            ):
                return []
            text = entries.message_text(record)
            mark = " (steer)" if fields.get("steer") else ""
            if entries.interaction(record):
                return [f"{interaction_label(record)}> {text}"]
            return [f"{who(source, self.source)}{mark}> {text}"]
        if kind == "chat/assistant_reply":
            content = entries.assistant_text(record)
            lines = [content] if content else []
            summaries = record.get("call_summaries")
            for call in entries.tool_calls(record):
                lines.append(f"> {call_line(call, summaries)}")
            return lines
        if kind == "tools/result":
            return clipped(fields.get("result") or "")
        if kind == "sessions/ask":
            # The CLI's box holds the question; the transcript keeps one line
            return [ask_line(record)]
        if kind == "sessions/answer":
            if entries.interaction(record):
                return [f"({interaction_label(record)}: {fields.get('text', '')})"]
            by = who(fields.get("source"), self.source)
            if fields.get("kind") == "secret":
                return [f"({by}: {fields.get('text', '')})"]
            if fields.get("kind") == "question":
                return [f"({by} answered: {fields.get('text', '')})"]
            if fields.get("yes"):
                return [f"({by}: approved)"]
            return [f"({by} replied; not run)"]
        if kind in entries.NOTE_TYPES:
            self.restarts += fields.get("kind") == "restarted"
            return [fields.get("text", "")]
        if kind == "sessions/switch_note":
            return [f"({switch_text(record)})"]
        if kind in COMPACTION_RECORDS:
            return [f"({compaction_text(record)})"]
        if kind == "sessions/rewind_note":
            return [f"({rewind_text(record)})"]
        if kind == "sessions/turn_end":
            elapsed = duration(record, self.started, self.restarts)
            if fields.get("outcome") == "answered":
                return [f"({elapsed})"]
            why = fields.get("detail") or ""
            if fields.get("error"):
                why = fields["error"].strip().splitlines()[-1]
            detail = f": {why}" if why else ""
            return [f"({fields.get('outcome')}{detail}, {elapsed})"]
        return []
