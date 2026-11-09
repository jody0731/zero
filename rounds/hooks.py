"""The budget every turn runs under, and a round's end.

Budget: what a turn spends (agent time, and dollars at the model's price)
against the standing amount; past it, the turn's next call asks the user to
let it go on, and a yes gives it as much again. An estimate for work in
rounds past the standing amount, or a new budget for work under way, asks
the user too. Both are the user's in every mode, never the judge's: the
note is its own ("budget"), which the mode's approver doesn't answer, and
this hook puts it to the user (Park for the server). With nobody there
(the evals), a turn past its budget ends `budget_reached`. A round is
bounded by its run's budget instead: past it, the round ends
(`rounds_budget_spent`) and the run waits for a new one.

Rounds: at a round's end, its handoff becomes the next round's fresh
context (a compaction that keeps nothing, context/), and the server's
rounds queue the next round's message (service.py). A turn compacted twice
is told that work can go on in rounds. Nothing in a round waits on the user,
who is away while it goes on: a call that would ask them is refused, with
its reason.
"""

from core import history
from core.asking import DECLINED, PARK, ask_of, put_to_user
from core.hooks import Deny, Hook, Park, Stop
from model.servers import resolve
from rounds import tool as roundtools

REFUSED = (
    "Not run: {reason}, and nobody is here to answer."
)
# A call in a round that would wait on the user
UNASKED = (
    "Not run: {reason}, and the user isn't asked while work in rounds goes on."
)
BUDGET_REFUSED = (
    "Not run: this turn reached its budget and going on wasn't approved."
)
# A turn's second compaction: the fact that long work can run in rounds
OFFERED = (
    "[Harness note: this turn's context has been compacted twice. Work that "
    "goes on can also run in rounds (start_rounds): each round a turn whose "
    "context starts fresh from the last round's handoff, within a budget the "
    "user approves.]"
)
# A message the user sent with --rounds (session/send's `rounds`)
ASKED = (
    "[Harness note: the user sent this message to be worked on in rounds. No "
    "work in rounds is under way yet: start_rounds starts it, with its "
    "estimate and done check.]"
)
# Turn outcomes that end a round as failed, not by the user's say
FAILED = ("model_error", "crashed", "broken_replies", "judge_refusals",
          "hook_error", "failed", "cut_off")
# Turn outcomes by the user's say: the work in rounds stops
STOPPED = ("interrupted", "declined", "budget_reached")


def price_of(name):
    """A model's price (USD per 1M tokens: cache hit, miss, output), or None."""
    try:
        return resolve(name)[0].price
    except Exception:
        return None


def call_dollars(prompt, cached, completion, price):
    """One model call's cost at `price`; nothing without one."""
    if not price:
        return 0.0
    hit, miss, out = price
    prompt, cached, completion = prompt or 0, cached or 0, completion or 0
    return (cached * hit + (prompt - cached) * miss + completion * out) / 1e6


def turn_dollars(records, price):
    """What a turn's model calls cost: each at its own model's price (a call
    on the fallback model names it, core/loop.py)."""
    total = 0.0
    for record in records:
        if record["type"] != "assistant":
            continue
        own = price_of(record["fallback"]) if record.get("fallback") else price
        total += call_dollars(record.get("prompt_tokens"),
                              record.get("cached_tokens"),
                              record.get("completion_tokens"), own)
    return total


class Budget(Hook):
    """A turn's spend against the standing amount, and estimates for work in
    rounds, asked of the user. `ask`: how to ask (PARK for the server; None
    when nobody is there)."""

    def __init__(self, minutes, dollars, ask):
        self._minutes = minutes
        self._dollars = dollars
        self._ask = ask

    def turn_start(self, turn):
        """Rebuilt from the open turn's records: what it spent, and each yes to
        go on, so a parked or restarted turn keeps its raised budget."""
        session = turn.session
        records = history.unfinished_turn(session.log.branch())
        self._spent = turn_dollars(records, session.settings.get("price"))
        self._raised = 0
        self._refused = False
        self._asked_step = None
        for record in records:
            if record["type"] == "tool" and record.get("ran"):
                hit = record.get("notes", {}).get("budget")
                if hit and hit.get("kind") == "turn":
                    self._raised += 1

    def after_model(self, step):
        reply = step.reply
        model = step.turn.session.model
        price = getattr(getattr(model, "server", None), "price", None)
        self._spent += call_dollars(reply.prompt_tokens, reply.cached_tokens,
                                    reply.completion_tokens, price)

    def _limits(self):
        times = 1 + self._raised
        return self._minutes * times, self._dollars * times

    def before_tool(self, call):
        if self._refused:
            return Deny(BUDGET_REFUSED)
        if call.name == "start_rounds":
            self._estimate(call)
            return None
        # A round is its run's budget's to bound (step_end), not the turn's
        if running_round(call.turn) is not None:
            return None
        minutes, money = self._limits()
        used = call.turn.agent_seconds() / 60
        past = used >= minutes or self._spent >= money
        # Only a step's first call asks: its yes raises the budget for the
        # next, a refusal denies the rest
        if not past or self._asked_step == call.step.number:
            return None
        self._asked_step = call.step.number
        more = (f"{roundtools.duration(self._minutes)} and "
                f"{roundtools.dollars(self._dollars)}")
        spent = (f"run {roundtools.duration(used)} and spent "
                 f"{roundtools.dollars(self._spent)}")
        budget = f"{roundtools.duration(minutes)} and {roundtools.dollars(money)}"
        call.notes["budget"] = {
            "kind": "turn", "rule": "budget", "action": "ask",
            "reason": (f"this turn has {spent}, past its budget of {budget}; a "
                       f"yes gives it {more} more"),
            "about": (f"This turn has {spent}, past its budget of {budget}. A "
                      f"yes gives it {more} more."),
        }
        return None

    def _estimate(self, call):
        """start_rounds past the standing amount, or for work already under
        way, goes to the user."""
        args = call.args
        estimate = (args.get("rounds"), args.get("minutes"), args.get("dollars"))
        if roundtools.check_estimate(*estimate):
            return
        rounds = call.turn.session.tool_context.get("session_rounds")
        under_way = rounds is not None and rounds.open() is not None
        _, minutes, money = estimate
        if not under_way and minutes <= self._minutes and money <= self._dollars:
            return
        budget = roundtools.budget_text(int(estimate[0]), minutes, money)
        check = (f"; done check `{args['done_check']}`"
                 if args.get("done_check") else "; no done check")
        why = (
            "work in rounds is under way here, so a new budget is the user's"
            if under_way else
            f"it is past the standing amount of {roundtools.duration(self._minutes)} "
            f"and {roundtools.dollars(self._dollars)}"
        )
        call.notes["budget"] = {
            "kind": "estimate", "rule": "budget", "action": "ask",
            "reason": f"an estimate of {budget} for work in rounds: {why}",
            "about": (
                f"ZERO asks to work in rounds toward: \"{args.get('objective')}\". "
                f"Its estimate from now: {budget}{check}. {why[0].upper()}{why[1:]}."
            ),
        }

    def approve(self, call):
        hit = call.notes.get("budget")
        if not hit:
            return None
        why, about = f"budget: {hit['reason']}", hit["about"]
        # Another ask on the same call is named, so one yes never approves
        # what the user wasn't shown
        other = ask_of(call)
        if other:
            why += f"; and {other['reason']}"
            if other.get("about"):
                about += " " + other["about"]
        if self._ask is PARK:
            return Park(why, about)
        if self._ask is None:
            return Deny(REFUSED.format(reason=hit["reason"]))
        answer = put_to_user(call, self._ask, why)
        if answer is None:
            return Deny(REFUSED.format(reason=hit["reason"]))
        if not answer:
            return Deny(DECLINED)
        return None

    def after_tool(self, call):
        hit = call.notes.get("budget")
        if not hit or hit.get("kind") != "turn":
            return
        if call.ran:
            self._raised += 1
        elif not call.notes.get("cancelled"):
            self._refused = True

    def step_end(self, step):
        run = running_round(step.turn)
        if run is not None:
            used = step.turn.agent_seconds() / 60
            spent = (
                run["spent_minutes"] + used >= run["budget_minutes"]
                or run["spent_dollars"] + self._spent >= run["budget_dollars"] > 0
            )
            if spent:
                budget = roundtools.budget_text(
                    run["budget_rounds"], run["budget_minutes"],
                    run["budget_dollars"])
                return Stop("rounds_budget_spent",
                            f"{run['id']}'s budget of {budget} is spent")
            return None
        if self._refused:
            minutes, money = self._limits()
            return Stop(
                "budget_reached",
                f"{roundtools.duration(minutes)} and {roundtools.dollars(money)}, "
                "going on wasn't approved",
            )


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
    """Whether the turn started or gave a new budget to this run."""
    return any(
        record["type"] == "tool" and record.get("ran")
        and record.get("notes", {}).get("rounds_started") == run
        for record in records
    )


def running_round(turn):
    """The run this turn is a round of while the run goes on, or None: the
    turn that started it (round 1), or one its next round's message opened.
    The turn its spent budget opened isn't one: its new estimate is the
    user's to answer."""
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
    a turn compacted twice, and no ask in a round. `asks_user`: whether this
    mode's approver is the user (default, and bypassPermissions' asks); the
    budget's asks are the user's in every mode."""

    def __init__(self, asks_user=False):
        self._asks_user = asks_user

    def approve(self, call):
        if running_round(call.turn) is None:
            return None
        hit = call.notes.get("budget")
        if hit is None and self._asks_user:
            hit = ask_of(call)
        if hit:
            return Deny(UNASKED.format(reason=hit["reason"]))
        return None

    def turn_start(self, turn):
        session = turn.session
        if session.tool_context.get("session_rounds") is None:
            return
        records = history.unfinished_turn(session.log.branch())
        if not records or not records[0].get("rounds"):
            return
        if not any(r["type"] == "note" and r.get("kind") == "rounds_asked"
                   for r in records):
            session.write("note", kind="rounds_asked", text=ASKED)

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
        to its closing reply."""
        for call in step.calls:
            end = call.notes.get("round_end")
            if end and end["status"] == "going_on":
                step.turn.answer = (step.reply.content or "").strip() or None
                return Stop("answered", None)
        return None

    def turn_end(self, turn):
        """A round's end: its spend, then the next round, the budget spent,
        or the run's end. A turn that isn't one of its rounds (the user's
        message between them) changes nothing."""
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
        money = turn_dollars(records, session.settings.get("price"))
        state = rounds.next_state(run, turn.outcome, end, minutes, money)
        fresh = False
        if state in ("running", "waiting") and turn.outcome not in FAILED:
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
