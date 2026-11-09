"""A round's end, and ZERO told once when work in rounds passes its estimate.

Rounds: at a round's end, its handoff becomes the next round's fresh
context (a compaction that keeps nothing, context/), and the server's
rounds queue the next round's message (service.py). A turn compacted twice
is told that work can go on in rounds. Nobody watches work in rounds: in
default mode the judge answers what would have asked the user
(permissions/approval.py's DefaultApproval, which asks `running_round`).
ZERO's estimate is its own yardstick (6.7): past it, one note says so and
the work goes on; nothing stops at it.
"""

from core import history
from core.hooks import Hook, Stop
from rounds import tool as roundtools
from usage.spend import call_dollars, turn_dollars

# A turn's second compaction: the fact that long work can run in rounds
OFFERED = (
    "[Harness note: this turn's context has been compacted twice. Work that "
    "goes on can also run in rounds (start_rounds): each round a turn whose "
    "context starts fresh from the last round's handoff.]"
)
# A message the user sent with --rounds (session/send's `rounds`)
ASKED = (
    "[Harness note: the user sent this message to be worked on in rounds. No "
    "work in rounds is under way yet: start_rounds starts it, with its "
    "estimate and done check.]"
)
# Once, when the work passes ZERO's estimate
PAST_ESTIMATE = (
    "[Harness note: {id}, toward \"{objective}\", has used {spent}, past your "
    "estimate of {estimate}. The work goes on.]"
)
# Turn outcomes that end a round as failed, not by the user's say
FAILED = ("model_error", "crashed", "broken_replies", "judge_refusals",
          "hook_error", "failed", "cut_off")
# Turn outcomes by the user's say: the work in rounds stops
STOPPED = ("interrupted", "declined")


def round_end(records, run):
    """The round's end as its last end_round left it, or None."""
    found = None
    for record in records:
        if record["type"] != "tool" or not record.get("ran"):
            continue
        end = record.get("notes", {}).get("round_end")
        if end and end.get("run") == run:
            found = end
    return found


def started_in(records, run):
    """Whether the turn started or gave a new estimate to this run."""
    return any(
        record["type"] == "tool" and record.get("ran")
        and record.get("notes", {}).get("rounds_started") == run
        for record in records
    )


def running_round(turn):
    """The run this turn is a round of while the run goes on, or None: the
    turn that started it (round 1), or one its next round's message
    opened."""
    rounds = turn.session.tool_context.get("session_rounds")
    run = rounds.open() if rounds is not None else None
    if run is None or run["state"] != "running":
        return None
    if turn.inbox == run["round_inbox"]:
        return run
    records = history.unfinished_turn(turn.session.log.branch())
    return run if started_in(records, run["id"]) else None


class Rounds(Hook):
    """A round's end, the user's ask for rounds told, the offer of rounds to
    a turn compacted twice, and the note when the work passes its
    estimate."""

    def turn_start(self, turn):
        session = turn.session
        rounds = session.tool_context.get("session_rounds")
        if rounds is None:
            return
        records = history.unfinished_turn(session.log.branch())
        # What this turn's calls cost so far, for the estimate
        self._spent = turn_dollars(records, session.settings.get("price"),
                                   session.settings.get("fallback_price"))
        run = running_round(turn)
        if run is not None and run["round_count"] > run["estimate_rounds"]:
            self._past(turn, run, 0.0)
        if not records or not records[0].get("rounds"):
            return
        if not any(r["type"] == "note" and r.get("kind") == "rounds_asked"
                   for r in records):
            session.write("note", kind="rounds_asked", text=ASKED)

    def after_model(self, step):
        reply = step.reply
        model = step.turn.session.model
        price = getattr(getattr(model, "server", None), "price", None)
        self._spent = getattr(self, "_spent", 0.0) + call_dollars(
            reply.prompt_tokens, reply.cached_tokens, reply.completion_tokens,
            price)

    def _past(self, turn, run, minutes):
        """Tell ZERO once that the work passed its estimate."""
        if run["past_estimate_at"] is not None:
            return
        spent = roundtools.estimate_text(
            run["round_count"], run["spent_minutes"] + minutes,
            run["spent_dollars"] + getattr(self, "_spent", 0.0))
        estimate = roundtools.estimate_text(
            run["estimate_rounds"], run["estimate_minutes"],
            run["estimate_dollars"])
        turn.session.write("note", kind="past_estimate", run=run["id"],
                           text=PAST_ESTIMATE.format(
                               id=run["id"], objective=run["objective"],
                               spent=spent, estimate=estimate))
        turn.session.tool_context["session_rounds"].mark_past(run["id"])

    def before_model(self, step):
        session = step.turn.session
        rounds = session.tool_context.get("session_rounds")
        if rounds is None or rounds.open() is not None:
            return None
        records = history.unfinished_turn(session.log.branch())
        compactions = sum(r["type"] == "compaction" for r in records)
        offered = any(r["type"] == "note" and r.get("kind") == "rounds_offered"
                      for r in records)
        if compactions >= 2 and not offered:
            session.write("note", kind="rounds_offered", text=OFFERED)
        return None

    def step_end(self, step):
        """A round that goes on ends its turn with the step that called
        end_round: the next round starts fresh from that handoff, so no work
        is done after it that it leaves out. Done and stuck leave the turn
        to its closing reply. Work past its estimate is told once."""
        for call in step.calls:
            end = call.notes.get("round_end")
            if end and end["status"] == "going_on":
                step.turn.answer = (step.reply.content or "").strip() or None
                return Stop("answered", None)
        run = running_round(step.turn)
        if run is not None and run["past_estimate_at"] is None:
            minutes = step.turn.agent_seconds() / 60
            money = getattr(self, "_spent", 0.0)
            if (run["spent_minutes"] + minutes > run["estimate_minutes"]
                    or run["spent_dollars"] + money > run["estimate_dollars"] > 0):
                self._past(step.turn, run, minutes)
        return None

    def turn_end(self, turn):
        """A round's end: its spend, then the next round or the run's end. A
        turn that isn't one of its rounds (the user's message between them)
        changes nothing."""
        session = turn.session
        rounds = session.tool_context.get("session_rounds")
        if rounds is None:
            return
        run = rounds.open()
        if run is None:
            return
        records = history.unfinished_turn(session.log.branch())
        if not started_in(records, run["id"]) and turn.inbox != run["round_inbox"]:
            return
        end = round_end(records, run["id"])
        minutes = turn.agent_seconds() / 60
        money = turn_dollars(records, session.settings.get("price"),
                             session.settings.get("fallback_price"))
        state = rounds.next_state(run, turn.outcome, end)
        fresh = False
        if state == "running" and turn.outcome not in FAILED:
            session.compact("round", end["handoff"] if end else None)
            done = history.unfinished_turn(session.log.branch())
            cut = next((r for r in reversed(done) if r["type"] == "compaction"
                        and r.get("reason") == "round"), None)
            fresh = cut is not None
            if fresh and end is None:
                # The handoff the model wrote, kept as the round's own
                end = {"status": "going_on", "handoff": cut["summary"]}
        rounds.finish_round(run["id"], state, turn.outcome, end, minutes, money,
                            fresh)
