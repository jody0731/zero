"""`start_subagent`, `send_message` and `subagents`, and the texts the model
reads of subagents: a subagent's task, a message between sessions, and the
harness's notices of a subagent's turn it couldn't finish.

What a subagent is in the server (its session, its row, its notices)
belongs to the server that hands a session its `session_subagents`
(service.py); without one there are none, and the tools aren't offered.
"""

from core import history
from core.tools import tool
from subagents import SETTINGS
from usage.spend import describe, estimate_text, past_estimate

# Who a notice from or about another session comes from; the system message
# names who sends notices. "subagent": the harness, of a subagent's turn;
# "session": another session's own words
UP = "subagent"
history.NOTICES[UP] = (
    "one of your subagents: an answer it ended a turn with and didn't send "
    "you, a turn of its own it couldn't finish (a crash, a failure, a stop), "
    "or its use past your estimate"
)
ACROSS = "session"
history.NOTICES[ACROSS] = (
    "another session of ZERO's: a message it sent, or the task it started you "
    "with"
)

START_FROM = ("fork", "handoff")
# How deep subagents go where no server says (the inventory's copy)
DEFAULT_DEPTH = {s.name: s.default for s in SETTINGS}["max_subagent_depth"]
# A subagent's first message, the assignment of the session that started
# it: an instruction, as the parent's words, not a fact of the harness's.
# The report line comes last, after the parent's task, so nothing the parent
# writes follows it (DSH's order: in one run, a parent's "Prefer: your final
# turn ends with the full report text" below the line won over it)
TASK = (
    "You are a subagent of session {parent} (\"{title}\").\n\nYour task:\n"
    "{task}\n\nWhen you've finished the task, report back to session "
    "{parent} with send_message. It doesn't see your calls, their output or "
    "your reasoning, only what you send; sending doesn't end your turn."
)
# The harness's word to a parent on a turn its subagent couldn't finish
REPORT = "Subagent {id} (\"{title}\") {how}. In all it has used {use}."
# The safety net: a turn's answer the subagent didn't send its parent (its
# last call wasn't a message to the parent that went through), passed up
# once it rests (DSH's settlement notice)
UNSENT = (
    "Subagent {id} (\"{title}\") ended a turn with an answer it didn't send "
    "you. In all it has used {use}. Its answer:\n{answer}"
)
NO_ANSWER = (
    "Subagent {id} (\"{title}\") ended a turn with no answer and sent you "
    "nothing. In all it has used {use}."
)
# To the top session of the tree, the user's, when a subagent waits for the
# user's answer
WAITING = (
    "Subagent {id} (\"{title}\") of session {parent} is waiting for the "
    "user's answer to a question ({why})."
)
# Once, when a subagent's use passes the estimate it was started with
PAST_ESTIMATE = (
    "Subagent {id} (\"{title}\") has used {use}, past your estimate of "
    "{estimate}. It is still running."
)
# A message one session sends another
MESSAGE = "Session {id} (\"{title}\") sends:\n{text}"


def how_text(outcome, detail=None):
    """How a turn a subagent couldn't finish ended, as the notice says it."""
    if outcome == "interrupted":
        return f"was stopped ({detail or 'by a stop'})"
    tail = f": {detail}" if detail else ""
    return f"ended a turn without finishing it ({outcome}{tail})"


def use_text(row, use):
    """What a subagent has used in all (its own subagents' included),
    against its estimate."""
    estimate = (row["estimate_minutes"], row["estimate_weighted_tokens"])
    where = "past" if past_estimate(use, *estimate) else "of"
    return f"{describe(use)}, {where} your estimate of {estimate_text(*estimate)}"


def report_text(row, outcome, use, detail=None):
    """The harness's word on a turn a subagent couldn't finish: how it
    ended, and what it has used against its estimate."""
    return REPORT.format(id=row["session"], title=row["title"],
                         how=how_text(outcome, detail), use=use_text(row, use))


def unsent_text(row, use, answer):
    """A turn's answer the subagent didn't send its parent, passed up."""
    if not (answer or "").strip():
        return NO_ANSWER.format(id=row["session"], title=row["title"],
                                use=use_text(row, use))
    return UNSENT.format(id=row["session"], title=row["title"],
                         use=use_text(row, use), answer=answer)


def state_text(entry):
    """A session's state as `subagents` says it."""
    state = entry["state"]
    if entry.get("outcome") and state in ("idle", "failed"):
        state += f", its last turn {entry['outcome']}"
    return state


def tree_line(entry, indent=True):
    """One session of the tree, as `subagents` lists it, indented by its
    level."""
    indent = "  " * entry["depth"] if indent else ""
    who = ("the user's" if entry["started_by"] == "user"
           else f"a subagent, level {entry['depth']}")
    this = " (this session)" if entry["this"] else ""
    line = (f"{indent}- {entry['id']}{this} \"{entry['title']}\": {who}; "
            f"{state_text(entry)}; used {describe(entry['use'])}")
    if entry.get("estimate"):
        past = past_estimate(entry["use"], *entry["estimate"])
        line += (f", {'past' if past else 'of'} its estimate of "
                 f"{estimate_text(*entry['estimate'])}")
    return line


def describe_start(limits, context=None):
    """start_subagent's description, with how deep subagents go: the
    server's setting as it stands (session_subagents), which the tool is
    offered only while above 0."""
    found = (context or {}).get("session_subagents")
    depth = found.max_depth() if found is not None else DEFAULT_DEPTH
    if depth <= 1:
        deepest = "A subagent can't start its own."
    elif depth == 2:
        deepest = "A subagent's subagents can't start their own."
    else:
        deepest = (f"Subagents go {depth} levels below a session the user "
                   "started; those at the last level can't start their own.")
    return (
        "Start a subagent: a session of its own under this one, which works "
        "on `task` while you go on; several run side by side. Its calls and "
        "their outputs stay in its own context, and only its report comes "
        "into this one. It builds a context of its own, counted in this "
        "session's use, and what it found and didn't send stays in its "
        "session. It runs in this session's workspace, on "
        "its files, with this session's model; nobody watches it, so in "
        "default mode what would ask the user goes to the judge, which reads "
        "the user's words, and other modes run as here. It returns the "
        "subagent's id at once. Its first message holds `task`, then names "
        "this session and asks it to report back here with send_message when "
        "it has finished. The report reaches this session as a notice from "
        "\"session\": while this turn is running, at its next step; once it "
        "has ended, the notice starts a new one. A notice from \"subagent\" "
        "comes when one of its turns ends with an answer it didn't send you, "
        "which the notice carries, or ends without finishing (a crash, a "
        "failure, a stop). `start_from`: fork, it starts from "
        "a copy of this session's conversation up to its last finished turn "
        "(this turn not included) and then reads `task`: it knows what this "
        "session knew, and each of its steps reads that copy again, a prefix "
        "the model's server may already hold in its cache; or handoff, it "
        "starts with only `task`, which then holds everything it needs "
        "(files, findings, constraints): its context starts small, and "
        "everything it reads is new input. `minutes` and `weighted_tokens` "
        "are your estimate of what it needs, its own subagents "
        "included: one notice comes if its use passes them, and nothing stops "
        f"at it. {deepest}"
    )


def can_start(context):
    found = context.get("session_subagents")
    return found is not None and found.can_start()


def on_a_server(context):
    return context.get("session_subagents") is not None


def start_summary(args):
    """A call as an ask and the judge show it."""
    task = str(args.get("task", "")).strip().splitlines()
    return (f"start subagent ({args.get('start_from')}) "
            f"\"{args.get('title', '')}\": {task[0] if task else ''}")


# What subagents are for, beside the other means in the system message
# (prompt/instructions.py, MEANS_FACTS). Keeping a part of the work out of
# this context comes first: it needs no second subagent. Its cost stands
# beside what it gives: six subagents moved a task's reading to their own
# contexts and didn't cut it (1.8 times the weighted tokens, measured)
MEANS = (
    "subagents (`start_subagent`): a part of the work done outside this "
    "conversation. What a subagent reads and runs stays in its own context "
    "and only its report comes into this one, so this session's later steps "
    "read less; and several work side by side, so parts that don't need each "
    "other take the time of the longest part, not the sum. What a subagent "
    "reads is new input in a context it builds itself, counted in this "
    "session's use."
)


@tool(
    describe_start,
    summary=start_summary,
    ready=can_start,
    means=MEANS,
    # It changes nothing on the machine; the subagent's own calls are asked
    # about where they run
    needs_permission=lambda args: False,
    task=("string", "What the subagent is to do; for a handoff, with "
                    "everything it needs"),
    start_from=("string", "fork or handoff"),
    title=("string", "A few words the session list shows it by"),
    minutes=("number", "Minutes of working time it needs, your estimate"),
    weighted_tokens=("number", "Weighted tokens it needs, your estimate"),
)
def start_subagent(task, start_from, title, minutes, weighted_tokens,
                   session_subagents=None):
    """`session_subagents`: the session's, from its server (service.py)."""
    if session_subagents is None:
        return "Subagents aren't available in this session."
    if start_from not in START_FROM:
        return "Error: start_from is fork or handoff."
    for name, value in (("minutes", minutes),
                        ("weighted_tokens", weighted_tokens)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"Error: {name} must be a number."
        if value <= 0:
            return f"Error: {name} must be more than 0."
    if not str(task).strip():
        return "Error: task is empty."
    if not str(title).strip():
        return "Error: title is empty."
    found, why = session_subagents.start(
        str(task).strip(), start_from, " ".join(str(title).split()),
        float(minutes), float(weighted_tokens),
    )
    if found is None:
        return why
    how = ("from a copy of this session's finished turns" if start_from == "fork"
           else "with its task alone")
    return (
        f"Started subagent {found} {how}. Its report reaches this session as "
        "a notice when it has finished: while this turn is running, at its "
        "next step; once it has ended, the notice starts a new one."
    )


@tool(
    "Send a message to another session of ZERO's, by its id. It reaches "
    "that session as a notice from \"session\", with this session's id and "
    "title: a session at rest starts a turn on it, one with a turn running "
    "reads it at its next step, and one waiting for the user's answer reads "
    "it once the user has answered. A message never answers a question.",
    summary=lambda args: f"message to session {args.get('session')}",
    ready=on_a_server,
    # It changes nothing on the machine
    needs_permission=lambda args: False,
    session=("string", "The session's id"),
    text=("string", "The message"),
)
def send_message(session, text, session_subagents=None):
    if session_subagents is None:
        return "Messages to other sessions aren't available in this session."
    if not str(text).strip():
        return "Error: text is empty."
    return session_subagents.send(str(session).strip(), str(text).strip())


def subagents_summary(args):
    if args.get("stop") is True:
        return f"subagents: stop {args.get('id')}'s turn"
    if args.get("id"):
        return f"subagents: look at {args['id']}"
    return "subagents: this session tree"


@tool(
    "This session tree: the session the user started and every subagent "
    "under it, each with its id, title, level, its state (running, waiting "
    "for the user's answer, idle, failed), its last turn's outcome, and its "
    "use, its own subagents' included, against the estimate it was started "
    "with. With `id`: that session's state and the answer its last turn "
    "ended with, whole. `stop`: stop the turn a subagent below this session "
    "is running; its own subagents go on, and a notice of the stop reaches "
    "its parent.",
    optional=("id", "stop"),
    summary=subagents_summary,
    ready=on_a_server,
    changing=True,
    needs_permission=lambda args: args.get("stop") is True,
    id=("string", "A session of this tree"),
    stop=("boolean", "Stop the turn it is running"),
)
def subagents(id=None, stop=False, session_subagents=None):
    """`stop` is the model's argument, not the turn's stop flag (core/tools.py,
    run)."""
    if session_subagents is None:
        return "Subagents aren't available in this session."
    if id is None:
        return "\n".join(tree_line(entry) for entry in session_subagents.tree())
    id = str(id).strip()
    if stop is True:
        return session_subagents.stop(id)
    entry = session_subagents.look(id)
    if isinstance(entry, str):
        return entry
    lines = [tree_line(entry, indent=False)]
    if entry.get("answer"):
        lines += ["Its last turn's answer:", entry["answer"]]
    elif entry.get("ended"):
        lines.append("Its last turn ended with no answer.")
    else:
        lines.append("No turn of its own has ended yet.")
    return "\n".join(lines)
