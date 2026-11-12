"""Session, turn, step: the loop, with the log as its message history.

Every record is written before the model sees it. Every tool call gets a
result; Ctrl+C closes calls that never started and tells the model; a cut-off
reply gets a note; an empty reply is never taken as the answer; a malformed
call gets its error back. Hooks supply limits and decisions.
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
from core.history import LOG_FORMAT, NewerLog, OldLog, log_format
from core.hooks import Deny, HookRunner, Park, Stop
from core.llm import MODEL_ERRORS
from core.records import Call, ModelFailure, Step, Turn

CUT_OFF = "[Your last reply hit the output limit and was cut off.]"
INTERRUPTED = "[The user interrupted this turn.]"
NOT_STARTED = "Not run: the user interrupted the turn before this call started."
# A crash isn't the user's doing
CRASHED_FIRST = (
    "Not run: the turn stopped on an error in the harness before this call "
    "started."
)
EMPTY = "[Your last reply was empty.]"
# The session's model failed (past its retries, when the error is retried)
# and the turn goes on with the fallback model; the next turn tries the
# session's own again
FALLBACK = (
    "[Harness note: {main} failed ({error}), so this turn goes on with the "
    "fallback model, {fallback}.]"
)
MAIN_AGAIN = "[Harness note: this turn runs on {main} again, the session's model.]"

class Parked(Exception):
    """A call waits for the user's reply: the turn stops here, its `ask` record
    written, with no step record and no turn_end yet."""

    def __init__(self, record):
        super().__init__(record["ask"])
        self.record = record


class Session:
    """One conversation: the message history, shared by all its turns.

    Its system message is written once and kept, until the cache is lost
    anyway: a compaction, a model switch, or new code that changes what the
    model reads (`code_mark`). Then it is written again as things stand
    (`rebuild`, the system prompt's start read again: the user's files)."""

    def __init__(self, model, hooks, system_prompt, log, settings, tool_context=None,
                 rebuild=None, code_mark=None, fallback=None):
        self.model = model
        # The session's own model, and how to find the one a turn goes on
        # with when it fails past its retries: `fallback()`, asked at the
        # failure, gives it or None (None: never one)
        self.main_model = model
        self.fallback = fallback
        self.hooks = HookRunner(hooks)
        self.log = log
        self.settings = settings
        # As built, before a rebuild takes the logged ones: what a switch logs
        self.fresh_settings = settings
        # What this session's tools get besides the model's arguments (its sandbox)
        self.tool_context = tool_context or {}
        # The system prompt's start: as built, and how to read it again
        self.system_start = system_prompt
        self.rebuild = rebuild
        # What the model reads of the code (its tools, the system message's
        # parts), as a short hash; None: never checked
        self.code_mark = code_mark
        records = log.branch()
        self.prompt_tokens = 0
        # Who asked for a compaction before the running turn's next step (the
        # user, through a server: want_compaction), until the context
        # component takes it (take_wanted_compaction)
        self.compaction_wanted_by = None
        if records:
            self.restore(log)
        else:
            self.messages = [{"role": "system", "content": self.system_text()}]

    def system_text(self):
        """The system message as it would be written now."""
        start = self.rebuild() if self.rebuild is not None else self.system_start
        return start + self.hooks.collect("system_prompt", self)

    def marked(self):
        """The code mark a record with a system message keeps."""
        return {"code_mark": self.code_mark()} if self.code_mark else {}

    def rebuild_system(self, why):
        """Write the system message as things stand now and take it from here
        on: `why` is "model" (a switch) or "code" (new code)."""
        self.write("system", system_prompt=self.system_text(), why=why,
                   **self.marked())
        self.messages = history.replay(self.log.branch())

    def compact(self, reason, handoff=None, fresh=False, occasion=None):
        """A compaction now, between turns: what the hooks' crashes say when
        it failed, else []. A turn's own come before its next model call.
        `reason`: who or what asks, as its record keeps it. `fresh`: the
        context keeps nothing but `handoff`, or one the model writes when
        there is none (a round's end, rounds/); `occasion`: what happened, in
        the asker's words, which the model reads where the compaction
        happened ("a round ended")."""
        errors = self.hooks.take_errors()
        self.hooks.notify("compaction_asked", self, reason, handoff, fresh,
                          occasion)
        found = self.hooks.take_errors()
        # A round's end comes inside a turn: its earlier errors stay its own
        self.hooks.errors = errors
        return found

    def want_compaction(self, by):
        """A compaction before the running turn's next step, asked for by
        `by` ("user"), which the context component makes then."""
        self.compaction_wanted_by = by

    def take_wanted_compaction(self):
        """Who asked for a compaction before this step, once; else None."""
        by, self.compaction_wanted_by = self.compaction_wanted_by, None
        return by

    def compacted(self, *, reason, summary, kept_from, tokens_before=None,
                  line=None, cleared=None, tokens_after=None, handoff_at=None,
                  handoff_call=None, saved=None, cleared_only=False,
                  fresh=False, occasion=None):
        """Write a compaction, with the system message as things stand now,
        and take the context it leaves. Its fields, as history replays them:
        who or what asked (`reason`), the tokens before and after and the
        `line` the context is kept under, the handoff (`summary`, written at
        `handoff_at`, by `handoff_call`), the first record kept
        (`kept_from`), the outputs cleared ({seq: the line in their place})
        and the files they were saved in (`saved`), whether it only cleared
        outputs (`cleared_only`) or kept nothing but the handoff (`fresh`,
        on an `occasion` the model reads)."""
        fields = {
            "reason": reason, "tokens_before": tokens_before, "line": line,
            "summary": summary, "kept_from": kept_from, "cleared": cleared,
            "tokens_after": tokens_after, "handoff_at": handoff_at,
            "handoff_call": handoff_call, "saved": saved,
            "cleared_only": cleared_only or None, "fresh": fresh or None,
            "occasion": occasion,
        }
        # Only what it has, as before; its handoff always, None when it
        # only cleared outputs and had none to carry
        fields = {k: v for k, v in fields.items()
                  if v is not None or k == "summary"}
        record = self.write("compaction", system_prompt=self.system_text(),
                            **self.marked(), **fields)
        self.messages = history.replay(self.log.branch())
        return record

    def rewind(self, seq, line_chars, session_id=None):
        """Go back to before the user's message `seq`, one that starts a turn
        on the current branch: a `rewind` record whose parent is the record
        before it, its note saying what it leaves behind (core/history.py's
        rewind_note, each message's line `line_chars` long at most, with what
        the `rewound` hooks add), and the context the branch now holds. Files
        and memory stay as they are: the harness undoes nothing.
        `session_id`: the server's id for the session, which the note names.
        Returns (the record, the part left behind)."""
        parent, left = history.left_behind_by(self.log.branch(), seq)
        extra = self.hooks.collect("rewound", self, left)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        record = self.write(
            "rewind", parent=parent["seq"], left_from=left[0]["seq"],
            left_to=left[-1]["seq"],
            text=history.rewind_note(
                now, left, line_chars, extra, session_id,
                reader=tools.record_reader(self.tool_context),
            ),
        )
        self.messages = history.replay(self.log.branch())
        return record, left

    def tool_schemas(self):
        """The tools as this session tells them: bash's says its timeout."""
        return tools.schemas(self.tool_context.get("limits"), self.tool_context)

    def restore(self, log):
        """Restore the logged prompt and messages without rebuilding hooks."""
        records = log.branch()
        found = log_format(records) if records else None
        if found is not None and found > LOG_FORMAT:
            raise NewerLog(found)
        if found != LOG_FORMAT:
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
            "format": LOG_FORMAT,
            "model": self.model.name,
            "mode": self.settings["mode"],
            "settings": self.settings,
            "hooks": [repr(p) for p in self.hooks.hooks],
            "system_prompt": self.messages[0]["content"],
            **self.marked(),
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
                  continued=False, carried=None):
        """`continued`: the turn is a server's `continue`, its text a
        harness note the user asked for, not words they typed. `carried`:
        what the message carries beside its text, kept on its record
        (history.MESSAGE_FIELDS, and the fields a server's parts hand on: a
        message sent to be worked on in rounds, which rounds/ tells the
        model)."""
        records = self.log.branch()
        if not records:
            # The system message as things stand at the first turn, where
            # its code mark is taken too: what was handed to the session's
            # tools since it was built (a server's jobs, rounds and
            # subagents) changes what it says
            self.messages = [{"role": "system", "content": self.system_text()}]
            fields = self.session_record()
            self.messages = []
            self.write("session", **fields)
        elif self.code_mark is not None:
            # New code that changes what the model reads: the cache is lost
            # anyway, so the system message is written again
            if history.system_now(records).get("code_mark") != self.code_mark():
                self.rebuild_system("code")
        fields = {"text": text}
        if inbox is not None:
            fields["inbox"] = inbox
        if source is not None:
            fields["source"] = source
        if continued:
            fields["continue"] = True
        fields.update(carried or {})
        self.write("user", **self.fitted(fields))
        self.back_to_main(records)
        return Turn(self, text, inbox, source, control=control)

    def back_to_main(self, records):
        """A new turn runs on the session's own model; one the last turn left
        on the fallback says so, so the note before isn't read as standing."""
        self.model = self.main_model
        last = next((r for r in reversed(records) if r["type"] == "note"
                     and r.get("kind") in ("fallback", "main_again")), None)
        if last is not None and last["kind"] == "fallback":
            self.write("note", kind="main_again",
                       text=MAIN_AGAIN.format(main=self.main_model.name))

    def spare_model(self):
        """The fallback model a failing call may go on with now: None while
        the turn already runs on one, or where there is none."""
        if self.fallback is None or self.model is not self.main_model:
            return None
        return self.fallback()

    def use_fallback(self, error, spare):
        """The rest of this turn on the fallback model `spare`, said in a
        note."""
        self.model = spare
        self.write("note", kind="fallback", text=FALLBACK.format(
            main=self.main_model.name, fallback=spare.name,
            error=f"{type(error).__name__}: {error}",
        ))

    def run_turn(self, question):
        return self.run(self.open_turn(question))

    def resume(self, inbox=None, control=None):
        """Repair an unfinished turn, returning it ready for run()."""
        records = history.unfinished_turn(self.log.branch())
        if not records:
            raise ValueError("no open turn to resume")
        user = records[0]
        if inbox is not None and user.get("inbox") != inbox:
            raise ValueError("the queued message is not the log's open turn")
        answer = history.final_answer(records)
        past_end = answer is None and history.ended_fresh(records) is not None
        if past_end:
            answer = history.ended_fresh(records)
        if answer is None:
            # What the hooks add, before repair writes anything: the files
            # this turn touched that changed while it was cut off, say
            extra = self.hooks.collect("resumed", self, records)
            text = history.restart_note(records, extra=extra)
            self.close_dangling_calls(history.UNKNOWN, "unknown")
            lost = history.lost_reply(self.log.branch())
            if lost is not None:
                self.write_reply(lost)
            self.write("note", kind="restarted", text=text)
        self.back_to_main(self.log.branch())
        number = max((r.get("step", 0) for r in records), default=0)
        turn = Turn(
            self, user["text"], user.get("inbox"), user.get("source"), number,
            control=control,
        )
        # Its time up to the cut, the waits on the user not counted as the
        # agent's; the time it was cut off isn't the turn's
        cut = datetime.fromisoformat(records[-1]["time"])
        before = (cut - datetime.fromisoformat(user["time"])).total_seconds()
        waited = sum(r.get("waited", 0) for r in records if r["type"] == "answer")
        turn.started -= before
        turn.waiting = waited
        turn.answer = answer
        turn.cut_past_its_end = past_end
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
            elif turn.answer is not None and (turn.cut_past_its_end or not (
                turn.control is not None and turn.control.steers()
            )):
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
            extra = self.hooks.collect("interrupted", self)
            self.write("note", kind="interrupted", text=INTERRUPTED + extra)
            detail = "Ctrl+C"
            if isinstance(error, Stopped):
                detail = error.detail
                if detail is None and turn.control is not None:
                    detail = turn.control.stop_detail
            turn.outcome, turn.detail = "interrupted", detail
            self.take_steers(turn)
        except BaseException:
            self.close_dangling_calls(CRASHED_FIRST)
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
        stop = self.call_with_retries(step)
        if stop is not None:
            return stop
        reply = step.reply
        step.cut_off = reply.finish_reason == "length"
        step.calls = [Call(step, raw) for raw in reply.tool_calls]
        # No text and no call is an API hiccup (DeepSeek warns of them), not an
        # answer. It isn't kept in the messages, so the next step asks again
        step.empty = not step.calls and not (reply.content or "").strip()
        fields = step.assistant_record()
        if self.model is not self.main_model:
            # Its price and its window are the fallback's: the price goes with
            # the call, as the fallback may differ from turn to turn
            fields["fallback"] = self.model.name
            price = getattr(self.model, "price", None)
            if price is not None:
                fields["fallback_price"] = price
        self.write("assistant", **fields)
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

    def call_with_retries(self, step):
        """Call the model, retrying while a hook says Retry."""
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
                spare = self.spare_model() if retry is None else None
                if spare is not None:
                    self.use_fallback(e, spare)
                    attempt = 0
                    continue
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
            self.write("user", **self.fitted(history.steer_fields(steer)))
            turn.control.delivered(steer)

    def fitted(self, fields):
        """A user record's fields, a notice's text fitted by the hooks first
        (one past its share goes to a file), and marked `left_behind` when the
        record its sender started from (`started_from_seq`) is off the current branch:
        a rewind left it behind."""
        if not history.is_notice(fields):
            return fields
        fields = {**fields, "text": self.hooks.pipe("notice", fields["text"], self)}
        started_from = fields.get("started_from_seq")
        if started_from is not None and started_from not in {
            record["seq"] for record in self.log.branch()
        }:
            fields["left_behind"] = True
        return fields

    def call_model(self, step):
        """One streamed model call (core/llm.py, complete), on every path with
        the clock on the model's silence. With a server's control, a stop
        abandons the reply at once, so it never writes to the session, and
        each chunk's draft goes out to the server's watchers; a draft is
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
                answer = self.execute(call)
                call.fields, call.failed = answer.fields, answer.failed
                # Before the log keeps it: one past its share goes to a file
                call.result = self.hooks.pipe("tool_result", answer.text, call)
                # Where its output starts, while the text is the tool's own
                if answer.head and call.result == answer.text:
                    call.output_at = len(answer.head)
                if call.remarks:
                    call.result += "\n\n" + "\n".join(call.remarks)
        self.write("tool", **call.record())
        self.hooks.notify("after_tool", call)

    def park(self, call, decision):
        """Write the call's `ask` record and stop the turn there, its parts
        kept apart: the reason the user reads (the judge's, else the agent's
        own `reason` argument; "" for none) and, where the rule says it,
        what the ask is about. `why`, the rule's words with the reason, is
        what the models that answer or hear of the ask read."""
        command = tools.summary(call.name, call.args, call.arguments)
        asked = {}
        if decision.about is not None:
            asked["about"] = decision.about
        reason = decision.reason or str(call.args.get("reason") or "").strip()
        record = self.write(
            "ask", ask=f"ask-{secrets.token_hex(4)}", step=call.step.number,
            call=call.id, tool=call.name, command=command, why=decision.why,
            reason=reason, notes=call.notes, choices=choices(call.notes),
            **asked,
        )
        raise Parked(record)

    def reply(self, fields, control=None):
        """Take the reply to the parked call: write the `answer` record and return
        the turn, ready for run() to finish the call's step and go on.

        `fields`: ask (its id), yes (a plain yes, from the consent check), inbox,
        source, text, consent (the check's verdict or None).
        """
        records = self.log.branch()
        ask = history.parked_ask(records)
        if ask is None or ask["ask"] != fields["ask"]:
            raise ValueError(f"no open ask {fields['ask']!r} in this session")
        pending = history.unfinished_turn(records)
        user = pending[0]
        asked = datetime.fromisoformat(ask["time"])
        waited = (datetime.now().astimezone() - asked).total_seconds()
        before = (asked - datetime.fromisoformat(user["time"])).total_seconds()
        turn = Turn(
            self, user["text"], user.get("inbox"), user.get("source"), ask["step"],
            control=control,
        )
        # Its wall time from the question; the waits on the user, this one and
        # each earlier park's, not counted as the agent's
        earlier = sum(r.get("waited", 0) for r in pending if r["type"] == "answer")
        turn.started -= before + waited
        turn.waiting = waited + earlier
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
        """The call run: its tool's Result (core/tools.py)."""
        t0 = time.monotonic()
        call.ran = True
        try:
            # The call's own notes, where a file tool puts what it changed
            context = {**self.tool_context, "notes": call.notes}
            # The turn's own words, which a web search sends along
            context["request"] = call.turn.question
            if call.turn.control is not None:
                context["stop"] = call.turn.control.stop
                # What has come for the turn and waits for its next step,
                # for a call that holds the turn until something does
                context["steers"] = call.turn.control.steers
            return tools.run(call.name, call.args, context)
        except KeyboardInterrupt as error:
            call.failed = True
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
        records = history.unfinished_turn(self.log.branch())
        for fields in history.closing_results(records, result, closed):
            self.write("tool", **fields)


def duration(seconds):
    """63 minutes, 2 hours, 3 days: how long an ask waited, for the model."""
    minutes = int(seconds // 60)
    for size, unit in ((1440, "day"), (60, "hour"), (1, "minute")):
        if minutes >= size * (2 if size > 1 else 1):
            count = minutes // size
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return f"{int(seconds)} seconds"
