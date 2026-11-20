"""The records of a session's work: a turn, its steps, their tool calls.

The loop (core/loop.py) fills them in; hooks read them at every hook.
"""

import time

from core import public_reply, tools


class Turn:
    def __init__(
        self, session, question, inbox=None, source=None, number=0, control=None
    ):
        self.session = session
        self.question = question
        self.inbox = inbox
        self.source = source
        self.number = number
        self.control = control
        self.started = time.monotonic()
        # Time spent waiting for the user, which hooks add (Approval)
        self.waiting = 0.0
        # A parked turn coming back: (step, call, later calls, the reply)
        self.unpark = None
        # A turn cut off after its end's fresh compaction: it had ended, and
        # a message that came since starts the next turn, not this one
        self.cut_past_its_end = False
        self.reply_format_errors = 0
        self.unformatted_reply = None
        self.outcome = self.detail = self.answer = self.error = None
        self.model_failure = None
        self.reply = None
        self.errors = []

    def agent_seconds(self):
        return time.monotonic() - self.started - self.waiting

    def wall_seconds(self):
        return time.monotonic() - self.started

    def record(self):
        fields = {
            "outcome": self.outcome,
            "seconds": round(self.wall_seconds(), 1),
            "agent_seconds": round(self.agent_seconds(), 1),
            "wait_seconds": round(self.waiting, 1),
        }
        for name in ("detail", "answer", "error", "inbox", "model_failure"):
            if getattr(self, name) is not None:
                fields[name] = getattr(self, name)
        if self.reply is not None and self.answer is not None:
            fields["reply"] = self.reply
        if self.errors:
            fields["hook_errors"] = self.errors
        return fields


class Step:
    def __init__(self, turn, number):
        self.turn = turn
        self.number = number
        self.started = time.monotonic()
        self.reply = None
        self.max_output_tokens = None
        self.retries = 0
        self.calls = []
        self.cut_off = self.malformed = self.empty = False
        self.sections = None
        self.invalid_reply = False
        self.seconds = 0.0
        self.errors = []

    def read_public_reply(self):
        """Separate public text from tool calls before either reaches the log."""
        reply = self.reply
        raw = reply.content or ""
        self.cut_off = reply.finish_reason == "length"
        self.sections = public_reply.parse(raw)
        repairing = self.turn.reply_format_errors > 0
        if repairing and (reply.tool_calls or self.cut_off):
            # A formatting correction cannot execute another operation.
            reply.tool_calls = []
            reply.message.pop("tool_calls", None)
            self.sections = None
        self.calls = [Call(self, call) for call in reply.tool_calls]
        self.empty = not self.calls and self.sections is None
        self.invalid_reply = self.empty and (
            repairing or (not self.cut_off and bool(raw.strip())))
        reply.content = None
        reply.message.pop("content", None)
        if self.invalid_reply:
            self.turn.reply_format_errors += 1
            self.turn.unformatted_reply = raw
        elif self.sections is not None:
            reply.content = public_reply.plain(self.sections)
            reply.message["content"] = reply.content
            self.turn.reply_format_errors = 0
            self.turn.unformatted_reply = None

    def assistant_record(self):
        reply = self.reply
        fields = {
            "step": self.number,
            "message": reply.message,
            "finish_reason": reply.finish_reason,
            "prompt_tokens": reply.prompt_tokens,
            "cached_tokens": reply.cached_tokens,
            "completion_tokens": reply.completion_tokens,
            "retries": self.retries,
            "model_seconds": round(reply.seconds, 1),
        }
        if self.empty:
            fields["empty"] = True
        if self.invalid_reply:
            fields["invalid_reply"] = True
        if self.sections is not None:
            fields["reply"] = self.sections
        return fields

    def record(self):
        fields = {"step": self.number, "seconds": round(self.seconds, 1)}
        if self.errors:
            fields["hook_errors"] = self.errors
        return fields


class Call:
    def __init__(self, step, raw):
        self.step = step
        self.turn = step.turn
        self.id = raw.id
        self.name = raw.function.name
        self.arguments = raw.function.arguments
        # A call to a tool that calls a found tool by its name is that
        # tool's call: every check, ask and record reads the tool that runs,
        # and `called_through` keeps the tool the model called
        self.called_through = None
        inner = tools.unwrapped(self.name, self.arguments)
        if inner is not None:
            self.called_through = self.name
            self.name, self.arguments = inner
        self.key = (self.name, tools.normalise(self.arguments))
        # A malformed call has `error` set and is answered without running
        self.args, self.error = tools.check(self.name, self.arguments)
        self.result = None
        self.denied = self.ran = False
        # When its tool started running (time.monotonic), once it has
        self.started_at = None
        # What its tool's answer carries beside its text (core/tools.py's
        # Result): its fields, whether it failed, and where its output
        # starts in `result` while that is the tool's own text
        self.fields = {}
        self.failed = False
        self.output_at = None
        # The file its whole result was saved in, past its share of the
        # context (context/'s tool_result pipe), for an app that links it
        self.saved_path = None
        # What hooks found out about this call (a rule hit, say); logged as is
        self.notes = {}
        # Hook notes for the model, added below the result of a call that ran
        self.remarks = []

    def record(self):
        return {
            "step": self.step.number,
            "call": self.id,
            "tool": self.name,
            "arguments": self.arguments,
            "result": self.result,
            "bad_call": bool(self.error),
            "denied": self.denied,
            "ran": self.ran,
            "notes": self.notes,
            # Only where there is something to say, as an older log's
            # records have none of them
            **({"fields": self.fields} if self.fields else {}),
            **({"failed": True} if self.failed else {}),
            **({"output_at": self.output_at}
               if self.output_at is not None else {}),
            **({"called_through": self.called_through}
               if self.called_through is not None else {}),
            **({"saved_path": str(self.saved_path)}
               if self.saved_path is not None else {}),
        }


class ModelFailure:
    def __init__(self, step, error, attempt):
        self.step = step
        self.error = error
        self.attempt = attempt
