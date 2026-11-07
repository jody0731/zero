"""The records of a session's work: a turn, its steps, their tool calls.

The loop (core/loop.py) fills them in; hooks read them at every hook.
"""

import time

from core import tools


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
        self.outcome = self.detail = self.answer = self.error = None
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
        for name in ("detail", "answer", "error", "inbox"):
            if getattr(self, name) is not None:
                fields[name] = getattr(self, name)
        if self.errors:
            fields["hook_errors"] = self.errors
        return fields


class Step:
    def __init__(self, turn, number):
        self.turn = turn
        self.number = number
        self.started = time.monotonic()
        self.reply = None
        self.retries = 0
        self.calls = []
        self.cut_off = self.malformed = self.empty = False
        self.seconds = 0.0
        self.errors = []

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
        self.key = (self.name, tools.normalise(self.arguments))
        # A malformed call has `error` set and is answered without running
        self.args, self.error = tools.check(self.name, self.arguments)
        self.result = None
        self.denied = self.ran = False
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
        }


class ModelFailure:
    def __init__(self, step, error, attempt):
        self.step = step
        self.error = error
        self.attempt = attempt
