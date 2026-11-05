"""Session, turn, step: the loop, with the log as its message history.

Every record is written before the model sees it. Every tool call gets a
result; Ctrl+C closes calls that never started and tells the model; a cut-off
reply gets a note; an empty reply is never taken as the answer; a malformed
call gets its error back. Plugins supply limits and decisions.
"""

import copy
import secrets
import time
import traceback
from datetime import datetime
from types import SimpleNamespace

from core import history, tools
from core.asking import (
    APPROVED, APPROVED_LATE, CANCELLED, DECLINED_STOP, NO, NOT_ASKED, choices,
)
from core.control import Stopped
from core.history import FORMAT, OldLog, version
from core.hooks import Deny, Hooks, Park, Stop
from core.llm import MODEL_ERRORS
from core.records import Call, ModelFailure, Step, Turn

CUT_OFF = "[Your last reply hit the output limit and was cut off.]"
INTERRUPTED = "[The user interrupted this turn.]"
NOT_STARTED = "Not run: the user interrupted the turn before this call started."
EMPTY = "[Your last reply was empty.]"

class Parked(Exception):
    """A call waits for the user's reply: the turn stops here, its `ask` record
    written, with no step record and no turn_end yet."""

    def __init__(self, record):
        super().__init__(record["ask"])
        self.record = record


class Session:
    """One conversation: the message history, shared by all its turns."""

    def __init__(self, model, plugins, system_prompt, log, settings, tool_context=None):
        self.model = model
        self.hooks = Hooks(plugins)
        self.log = log
        self.settings = settings
        # As built, before a rebuild takes the logged ones: what a switch logs
        self.fresh_settings = settings
        # What this session's tools get besides the model's arguments (its sandbox)
        self.tool_context = tool_context or {}
        records = log.records()
        self.prompt_tokens = 0
        if records:
            self.rebuild(log)
        else:
            system = system_prompt + self.hooks.collect("system_prompt", self)
            self.messages = [{"role": "system", "content": system}]

    def tool_schemas(self):
        """The tools as this session tells them: bash's says its timeout."""
        return tools.schemas(self.tool_context.get("limits"), self.tool_context)

    def rebuild(self, log):
        """Restore the logged prompt and messages without rebuilding plugins."""
        records = log.records()
        if not records or version(records) != FORMAT:
            raise OldLog("This session needs a log upgrade before it can run")
        self.log = log
        self.settings = history.settings_now(records)
        self.messages = history.replay(records)
        # Prompt size of the last model call, also after a rebuild
        self.prompt_tokens = next(
            (r["prompt_tokens"] for r in reversed(records) if r["type"] == "assistant"),
            0,
        )

    def session_record(self):
        return {
            "format": FORMAT,
            "model": self.model.name,
            "mode": self.settings["mode"],
            "settings": self.settings,
            "plugins": [repr(p) for p in self.hooks.plugins],
            "system_prompt": self.messages[0]["content"],
        }

    def write(self, type, **fields):
        """Append once, then project the record into the model's history."""
        self.log.path.parent.mkdir(parents=True, exist_ok=True)
        record = self.log.append(type, **fields)
        message = history.message(record)
        if message is not None:
            self.messages.append(message)
        return record

    def open_turn(self, text, inbox=None, source=None, control=None,
                  continued=False):
        """`continued`: the turn is a host's `continue`, its text a
        harness note the user asked for, not words they typed."""
        if not self.log.records():
            fields = self.session_record()
            self.messages = []
            self.write("session", **fields)
        fields = {"text": text}
        if inbox is not None:
            fields["inbox"] = inbox
        if source is not None:
            fields["source"] = source
        if continued:
            fields["continue"] = True
        self.write("user", **fields)
        return Turn(self, text, inbox, source, control=control)

    def run_turn(self, question):
        return self.run(self.open_turn(question))

    def resume(self, inbox=None, control=None):
        """Repair an unfinished turn, returning it ready for run()."""
        records = history.pending(self.log.records())
        if not records:
            raise ValueError("no open turn to resume")
        user = records[0]
        if inbox is not None and user.get("inbox") != inbox:
            raise ValueError("the queued message is not the log's open turn")
        answer = history.final_answer(records)
        if answer is None:
            # What the plugins add, before repair writes anything: the files
            # this turn touched that changed while it was cut off, say
            extra = self.hooks.collect("resumed", self, records)
            text = history.restart_note(records, extra=extra)
            self.close_dangling_calls(history.UNKNOWN, "unknown")
            lost = history.lost_reply(self.log.records())
            if lost is not None:
                self.write_reply(lost)
            self.write("note", kind="restarted", text=text)
        number = max((r.get("step", 0) for r in records), default=0)
        turn = Turn(
            self, user["text"], user.get("inbox"), user.get("source"), number,
            control=control,
        )
        turn.answer = answer
        return turn

    def run(self, turn):
        """Step until answered, stopped or parked. Record crashes, then re-raise
        them. A parked turn returns "waiting" with no turn_end."""
        try:
            self.hooks.notify("turn_start", turn)
            if turn.control is not None:
                turn.control.check()
            stop = None
            if turn.unpark is not None:
                stop = self.finish_parked(turn)
            elif turn.answer is not None and not (
                turn.control is not None and turn.control.steers()
            ):
                turn.outcome = "answered"
                return turn.outcome
            while stop is None:
                turn.number += 1
                stop = self.run_step(Step(turn, turn.number))
            turn.outcome, turn.detail = stop.outcome, stop.detail
        except Parked:
            turn.outcome = "waiting"
        except KeyboardInterrupt as error:
            self.close_dangling_calls()
            # Otherwise the model thinks the unfinished task went fine
            self.write("note", kind="interrupted", text=INTERRUPTED)
            detail = "Ctrl+C"
            if isinstance(error, Stopped):
                detail = error.detail
                if detail is None and turn.control is not None:
                    detail = turn.control.stop_detail
            turn.outcome, turn.detail = "interrupted", detail
            self.take_steers(turn)
        except BaseException:
            self.close_dangling_calls()
            turn.outcome, turn.error = "crashed", traceback.format_exc()
            raise
        finally:
            if turn.outcome != "waiting":
                turn.errors = self.hooks.take_errors()
                self.hooks.notify("turn_end", turn)
                turn.errors += self.hooks.take_errors()
                self.write("turn_end", **turn.record())
        return turn.outcome

    def run_step(self, step):
        """One model call plus its tool calls. Returns a Stop, or None."""
        stop = self.ask_model(step)
        if stop is not None:
            return stop
        reply = step.reply
        step.cut_off = reply.finish_reason == "length"
        step.calls = [Call(step, raw) for raw in reply.tool_calls]
        # No text and no call is an API hiccup (DeepSeek warns of them), not an
        # answer. It isn't kept in the messages, so the next step asks again
        step.empty = not step.calls and not (reply.content or "").strip()
        self.write("assistant", **step.assistant_record())
        bad_calls = bool(step.calls) and all(c.error for c in step.calls)
        step.malformed = step.cut_off or bad_calls or step.empty
        stop = self.run_calls(
            step, step.calls, lambda: self.hooks.notify("after_model", step)
        )
        if step.cut_off:
            self.write("note", kind="cut_off", text=CUT_OFF)
        elif step.empty:
            self.write("note", kind="empty", text=EMPTY)
        elif not step.calls:
            # No tool call means the model is answering
            step.turn.answer = reply.content
            if step.turn.control is not None:
                if step.turn.control.steers():
                    return None
            return Stop("answered", None)
        return stop

    def run_calls(self, step, calls, first):
        """`first()`, then each call in order, then the step record and step_end's
        decision. A parked call leaves the step open: no record until it ends."""
        parked = False
        try:
            first()
            for call in calls:
                self.run_call(call)
        except Parked:
            parked = True
            raise
        finally:
            # Also on Ctrl+C, so an interrupted step is still logged
            if not parked:
                step.seconds = time.monotonic() - step.started
                step.errors = self.hooks.take_errors()
                stop = self.hooks.decide("step_end", step)
                step.errors += self.hooks.take_errors()
                self.write("step", **step.record())
        return stop

    def ask_model(self, step):
        """Call the model, retrying while a plugin says Retry."""
        attempt = 0
        while True:
            if step.turn.control is not None:
                step.turn.control.check()
                self.take_steers(step.turn)
            stop = self.hooks.decide("before_model", step)
            if stop is not None:
                return stop
            try:
                step.reply = self.call_model(step)
            except MODEL_ERRORS as e:
                failure = ModelFailure(step, e, attempt)
                retry = self.hooks.decide("model_error", failure)
                if retry is None:
                    return Stop("model_error", f"{type(e).__name__}: {e}")
                # Logged, so a client watching can say why the model is slow
                #; the model's history leaves it out
                self.write(
                    "retry", step=step.number, retry=attempt + 1, error=str(e),
                    delay=round(retry.delay, 1),
                )
                if step.turn.control is None:
                    time.sleep(retry.delay)
                else:
                    step.turn.control.stop.wait(retry.delay)
                attempt += 1
                continue
            step.retries = attempt
            self.prompt_tokens = step.reply.prompt_tokens
            return None

    def take_steers(self, turn):
        if turn.control is None:
            return
        for steer in turn.control.steers():
            self.write("user", **history.steer_fields(steer))
            turn.control.delivered(steer)

    def call_model(self, step):
        """One streamed model call (core/llm.py, complete), on every path with
        the clock on the model's silence. With a host's control, a stop
        abandons the reply at once, so it never writes to the session, and
        each chunk's draft goes out to the host's watchers; a draft is
        never a record."""
        control = step.turn.control
        check = on_delta = None
        if control is not None:
            check = control.check

            def on_delta(draft):
                control.draft(step.number, draft)

        # The stream's reader may still be sending these after a stop, while
        # the loop goes on writing
        messages = copy.deepcopy(self.messages)
        return self.model.call(
            messages, self.tool_schemas(), check=check, on_delta=on_delta
        )

    def run_call(self, call, approved=None):
        """Answer one tool call: its error, a denial, or the tool's result.

        `approved`: a parked call the user said yes to, with the remark for the
        model; approve isn't asked again, but before_tool checks it again.
        """
        if call.turn.control is not None:
            call.turn.control.check()
        if call.error:
            call.result = call.error
        else:
            decision = self.hooks.decide("before_tool", call)
            # The human is only asked about calls every check let through
            if not isinstance(decision, Deny) and approved is None:
                decision = self.hooks.decide("approve", call)
            if isinstance(decision, Park):
                self.park(call, decision)
            if approved is not None and not isinstance(decision, Deny):
                call.remarks.append(approved)
            if isinstance(decision, Deny):
                call.denied, call.result = True, decision.reason
            else:
                if call.turn.control is not None:
                    call.turn.control.check()
                call.result = self.execute(call)
                if call.remarks:
                    call.result += "\n\n" + "\n".join(call.remarks)
        self.write("tool", **call.record())
        self.hooks.notify("after_tool", call)

    def park(self, call, decision):
        """Write the call's `ask` record and stop the turn there. Beside `why`,
        what the ask is about and whose reason the user reads: the
        judge's, else the agent's own `reason` argument."""
        command = tools.summary(call.name, call.args, call.arguments)
        asked = {}
        if decision.about is not None:
            asked["about"] = decision.about
        reason = decision.reason or str(call.args.get("reason") or "").strip()
        if reason:
            asked["reason"] = reason
        record = self.write(
            "ask", ask=f"ask-{secrets.token_hex(4)}", step=call.step.number,
            call=call.id, tool=call.name, command=command, why=decision.why,
            notes=call.notes, choices=choices(call.notes), **asked,
        )
        raise Parked(record)

    def reply(self, fields, control=None):
        """Take the reply to the parked call: write the `answer` record and return
        the turn, ready for run() to finish the call's step and go on.

        `fields`: ask (its id), yes (a plain yes, from the consent check), inbox,
        source, text, consent (the check's verdict or None).
        """
        records = self.log.records()
        ask = history.parked(records)
        if ask is None or ask["ask"] != fields["ask"]:
            raise ValueError(f"no open ask {fields['ask']!r} in this session")
        pending = history.pending(records)
        user = pending[0]
        asked = datetime.fromisoformat(ask["time"])
        waited = (datetime.now().astimezone() - asked).total_seconds()
        before = (asked - datetime.fromisoformat(user["time"])).total_seconds()
        turn = Turn(
            self, user["text"], user.get("inbox"), user.get("source"), ask["step"],
            control=control,
        )
        # Its wall time from the question, the wait not counted as the agent's
        turn.started -= before + waited
        turn.waiting = waited
        step = Step(turn, ask["step"])
        message = next(
            r["message"] for r in pending
            if r["type"] == "assistant" and r["step"] == ask["step"]
        )
        step.calls = [
            Call(step, SimpleNamespace(id=raw["id"], function=SimpleNamespace(
                name=raw["function"]["name"], arguments=raw["function"]["arguments"],
            )))
            for raw in message["tool_calls"]
        ]
        answered = {r["call"] for r in pending if r["type"] == "tool"}
        index = next(i for i, c in enumerate(step.calls) if c.id == ask["call"])
        call = step.calls[index]
        call.notes.update(ask.get("notes", {}))
        later = [c for c in step.calls[index + 1:] if c.id not in answered]
        # Written once the step is rebuilt, so the log never holds an answer the
        # turn couldn't act on
        self.write("answer", call=ask["call"], waited=round(waited, 1), **fields)
        turn.unpark = (step, call, later, {**fields, "waited": waited})
        return turn

    def finish_parked(self, turn):
        """The parked call's step, finished: on a yes the call runs (before_tool
        checks it again) and so do the calls after it; on the box's No
        it is declined, the rest are closed unstarted, and the turn ends;
        otherwise the call gets CANCELLED, the rest are closed unstarted, and
        the reply joins the turn."""
        step, call, later, fields = turn.unpark
        turn.unpark = None
        if fields.get("choice") == NO:
            def decline():
                call.result, call.denied = DECLINED_STOP, True
                self.write("tool", **call.record())
                self.hooks.notify("after_tool", call)
                self.close_dangling_calls(NOT_ASKED, "not_started")

            self.run_calls(step, [], decline)
            return Stop("declined", "the user said no")
        if fields["yes"]:
            waited = fields["waited"]
            remark = APPROVED
            if waited >= 60:
                remark = APPROVED_LATE.format(waited=duration(waited))
            return self.run_calls(
                step, later, lambda: self.run_call(call, approved=remark)
            )

        def cancel():
            call.result, call.notes["cancelled"] = CANCELLED, True
            self.write("tool", **call.record())
            self.hooks.notify("after_tool", call)
            self.close_dangling_calls(NOT_ASKED, "not_started")

        stop = self.run_calls(step, [], cancel)
        self.write_reply(fields)
        return stop

    def write_reply(self, fields):
        """A reply that wasn't a yes joins the turn as the user's message."""
        record = {"text": fields["text"], "steer": True, "inbox": fields["inbox"]}
        if fields.get("source") is not None:
            record["source"] = fields["source"]
        self.write("user", **record)

    def execute(self, call):
        t0 = time.monotonic()
        call.ran = True
        try:
            # The call's own notes, where a file tool puts what it changed
            context = {**self.tool_context, "notes": call.notes}
            # The turn's own words, which a web search sends along
            context["request"] = call.turn.question
            if call.turn.control is not None:
                context["stop"] = call.turn.control.stop
            return tools.run(call.name, call.args, context)
        except KeyboardInterrupt as error:
            call.result = (
                f"Interrupted by the user while running, after "
                f"{time.monotonic() - t0:.1f} s. It may have partly run, so its "
                "outcome is unknown."
            )
            call.result += getattr(error, "output_detail", "")
            self.write("tool", **call.record())
            self.hooks.notify("after_tool", call)
            raise

    def close_dangling_calls(self, result=NOT_STARTED, closed="not_started"):
        """Close calls without results: the API rejects an unmatched tool call.

        A call interrupted while running already got its own result in execute;
        the calls left on Ctrl+C never started.
        """
        records = history.pending(self.log.records())
        for fields in history.closures(records, result, closed):
            self.write("tool", **fields)


def duration(seconds):
    """63 minutes, 2 hours, 3 days: how long an ask waited, for the model."""
    minutes = int(seconds // 60)
    for size, unit in ((1440, "day"), (60, "hour"), (1, "minute")):
        if minutes >= size * (2 if size > 1 else 1):
            count = minutes // size
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return f"{int(seconds)} seconds"
