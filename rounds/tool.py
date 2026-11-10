"""`start_rounds` and `end_round`, and the texts the model reads of rounds:
a round's opening message, a run's end.

What a run is in the session's state (its row, the next round's message)
belongs to the server that hands a session its `session_rounds`
(service.py); without one there are no rounds.
"""

import subprocess
import tempfile
import time

from core import history
from core.tools import tool
from usage.spend import tokens_text

# A round's opening message to its session: a notice, not the user's
# words; the system message names who sends them
SOURCE = "rounds"
history.NOTICES[SOURCE] = "work in rounds"

STATUSES = ("going_on", "done", "stuck")
# A done check still running after this is stopped and counts as failed: a
# check that hangs would hold the round with nothing to show (a test suite
# takes minutes, not this)
CHECK_SECONDS = 30 * 60

# The program a done check runs in, as the shell's commands do
SHELL = "/bin/bash"

NOT_HERE = "Work in rounds isn't available in this session."
NO_RUN = "No work in rounds is under way in this session."


def duration(minutes):
    """Minutes as the model reads them: "45 min", "2 h 5 min"."""
    whole = round(minutes)
    if whole < 60:
        return f"{max(whole, 0)} min"
    hours, rest = divmod(whole, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def estimate_text(rounds, minutes, weighted_tokens):
    """An estimate, or what was used against one: "4 rounds, 45 min,
    1,200,000 weighted tokens"; no tokens for a run from before 0.6.8, whose
    estimate was in dollars."""
    text = f"{rounds} rounds, {duration(minutes)}"
    if weighted_tokens is None:
        return text
    return f"{text}, {tokens_text(weighted_tokens)}"


# What rounds are for, beside the other means in the system message
# (prompt/instructions.py, MEANS_FACTS)
MEANS = (
    "rounds (`start_rounds`): long work seen through from end to end. The "
    "server carries it from round to round, each reading a small context, "
    "until a check from outside says it is done; the user sees its estimate "
    "and how far it is."
)
DESCRIPTION = (
    "Work toward an objective in rounds. The server carries the work on "
    "from round to round with no message from the user. Each round is a turn "
    "of this session whose context starts fresh from the handoff the round "
    "before left with end_round, so it holds that round's work and not every "
    "earlier one's; the handoffs are kept in the session's folder, and the "
    "workspace stays as it is between rounds. So a round's steps read a "
    "small context however long the work gets; a round opens with only the "
    "system message in the cache, and reads again from the files what its "
    "handoff doesn't hold. Rounds run one after another, never side by "
    "side. Done is checked from outside: "
    "`done_check` is a command the server runs where this session's commands "
    "run when you report done, and exit code 0 is done. The turn that calls "
    "it is round 1; the server starts each next one, until the work is done, "
    "stuck or stopped by the user. While rounds go on, what would ask the "
    "user in default mode goes to the judge instead, which reads the user's "
    "words. The estimate (rounds, minutes of your own working time, "
    "weighted tokens) is yours: the work starts at once, nothing stops at "
    "the estimate, and one note comes if the work passes it."
)


def start_summary(args):
    """A call as an ask and the judge show it."""
    text = f"start rounds toward: {args.get('objective', '')}"
    if args.get("done_check"):
        text += f"; done check: `{args['done_check']}`"
    rounds, minutes, tokens = (
        args.get(k) for k in ("rounds", "minutes", "weighted_tokens"))
    if all(isinstance(v, (int, float)) for v in (rounds, minutes, tokens)):
        text += f"; estimate {estimate_text(int(rounds), minutes, tokens)}"
    return text


def check_estimate(rounds, minutes, weighted_tokens):
    """An error for an estimate that isn't three positive numbers, else
    None."""
    for name, value in (("rounds", rounds), ("minutes", minutes),
                        ("weighted_tokens", weighted_tokens)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"Error: {name} must be a number."
        if value <= 0:
            return f"Error: {name} must be more than 0."
    return None


@tool(
    DESCRIPTION,
    optional=("done_check",),
    summary=start_summary,
    # A means only where the session's server carries rounds on
    means=lambda context: (
        MEANS if context.get("session_rounds") is not None else None),
    # The done check is a command: in auto+ the judge sees it first, as it
    # sees bash's
    needs_permission=lambda args: bool(args.get("done_check")),
    objective=("string", "What the work is to achieve, in a sentence or two"),
    done_check=("string", "A command whose exit code 0 means the work is done, "
                          "e.g. a test suite"),
    rounds=("integer", "Rounds the work needs, in all from now"),
    minutes=("number", "Minutes of your working time it needs, in all from now"),
    weighted_tokens=("number", "Weighted tokens it needs, in all from now"),
)
def start_rounds(objective, rounds, minutes, weighted_tokens, done_check=None,
                 session_rounds=None, notes=None):
    """`session_rounds`: the session's rounds, from its server; None means
    there are none. While a run is open, a new estimate is its estimate from
    now. `notes`: the call's own,
    where the hooks find that this turn is one of the run's rounds."""
    if session_rounds is None:
        return NOT_HERE
    error = check_estimate(rounds, minutes, weighted_tokens)
    if error:
        return error
    if not str(objective).strip():
        return "Error: objective is empty."
    check = str(done_check).strip() if done_check else None
    run, extended = session_rounds.start(
        str(objective).strip(), check,
        (int(rounds), float(minutes), float(weighted_tokens)),
    )
    if notes is not None:
        notes["rounds_started"] = run["id"]
    estimate = estimate_text(run["estimate_rounds"], run["estimate_minutes"],
                             run["estimate_weighted_tokens"])
    if extended:
        return (
            f"The estimate of {run['id']} is now {estimate} in all, its "
            "use so far included. When end_round reports going_on, this "
            "turn ends with its step and the next round starts."
        )
    tail = (f" The done check: `{check}`." if check
            else " No done check: done is your word.")
    return (
        f"Started {run['id']}: this turn is round 1 of an estimated "
        f"{run['estimate_rounds']}, with your estimate of {estimate}.{tail} "
        "End each round with end_round: "
        "going_on ends this turn with its step, and the next round starts "
        "with a fresh context from your handoff."
    )


def run_check(command, sandbox, stop=None, now=time.monotonic):
    """The done check, run where the session's commands run: (exit code, its
    output, how long it took). A check past CHECK_SECONDS is stopped, exit
    code None; a stop ends it at once."""
    box = sandbox
    t0 = now()
    # The output to a file, not a pipe: a long one can't fill a pipe and hang
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(
            box.command([SHELL, "-c", command]), stdin=subprocess.DEVNULL,
            stdout=out, stderr=subprocess.STDOUT, env=box.env(),
            cwd=box.workspace, start_new_session=True,
        )
        code = None
        try:
            while True:
                try:
                    code = proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if stop is not None and stop.is_set():
                        raise KeyboardInterrupt
                    if now() - t0 > CHECK_SECONDS:
                        break
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        out.seek(0)
        text = out.read().decode("utf-8", errors="replace")
    return code, text, now() - t0


def check_text(command, code, output, seconds):
    if code is None:
        head = (f"The done check `{command}` was still running after "
                f"{duration(seconds / 60)} and was stopped")
    else:
        head = f"The done check `{command}` ended with exit code {code}"
    return f"{head}. Its output:\n{output.strip() or '(none)'}"


@tool(
    "End this round of the work in rounds. `status` is the whole "
    "objective's: going_on (it isn't met yet: this turn ends with this "
    "step, and the next round starts from the handoff), done (it is met: the "
    "server runs the done check first, and exit code 0 ends the work; with "
    "no check, done is your word) or stuck (the work stops there, and the "
    "user reads why). `handoff`: what "
    "the next round reads in place of this conversation: what was done and "
    "found, the state (files with their paths), what is next; for stuck, "
    "why. Paths, commands, numbers and names in it are copied exactly.",
    summary=lambda args: f"end round: {args.get('status')}",
    # It changes nothing on the machine; a done check was the judge's to see
    # when the work started
    needs_permission=lambda args: False,
    status=("string", "going_on, done or stuck"),
    handoff=("string", "What the next round reads in place of this one"),
)
def end_round(status, handoff, session_rounds=None, sandbox=None, notes=None,
              stop=None):
    """`notes`: the call's own, where the hooks find the round's end (its
    status, handoff and the check's outcome)."""
    if session_rounds is None:
        return NOT_HERE
    run = session_rounds.open()
    if run is None:
        return NO_RUN
    if status not in STATUSES:
        return f"Error: status is one of {', '.join(STATUSES)}."
    if not str(handoff).strip():
        return "Error: handoff is empty."
    end = {"run": run["id"], "status": status, "handoff": str(handoff).strip()}
    number = run["round_count"]
    text = ""
    if status == "done" and run["done_check"] and sandbox is not None:
        code, output, seconds = run_check(run["done_check"], sandbox, stop)
        shown = check_text(run["done_check"], code, output, seconds)
        path = session_rounds.save(run["id"], f"round-{number}-check.txt", shown)
        end["check"] = {"exit_code": code, "path": str(path)}
        if code != 0:
            end["status"] = "going_on"
            text = (
                f"{shown}\n\nSo the work goes on: this turn ends with this "
                f"step, and round {number + 1} starts; its message names {path}."
            )
        else:
            text = f"{shown}\n\nSo the work is done."
    elif status == "done":
        text = "No done check was set, so the work is done on your word."
    elif status == "stuck":
        text = "The work in rounds stops here, as stuck; the user reads your handoff."
    else:
        text = (
            f"Round {number} ended: this turn ends with this step, and round "
            f"{number + 1} starts with a fresh context from your handoff; the "
            "workspace stays as it is."
        )
    if notes is not None:
        notes["round_end"] = end
    return text


def went_on(early):
    """When the last round's context wasn't cut to its handoff (`early`: why,
    "ended early (model_error)"), what the next message says instead."""
    return (
        f"The last round {early}, so the context goes on from it rather than "
        "starting fresh"
    )


def round_text(run, folder, handoff_name, early=None):
    """The message that opens a round: the work, what it has used against
    ZERO's estimate, the done check's last result, and where the handoffs
    are. `early`: why the last round's context was kept (went_on), None when
    it wasn't."""
    number = run["round_count"]
    estimate_tokens = run["estimate_weighted_tokens"]
    spent = estimate_text(
        number - 1, run["spent_minutes"],
        None if estimate_tokens is None else run["spent_weighted_tokens"])
    estimate = estimate_text(run["estimate_rounds"], run["estimate_minutes"],
                             estimate_tokens)
    lines = [
        f"Round {number} of an estimated {run['estimate_rounds']} of "
        f"{run['id']}, toward: \"{run['objective']}\".",
        f"Used so far: {spent}, against your estimate of {estimate}.",
    ]
    if run["done_check"]:
        check = f"Done check: `{run['done_check']}`"
        if run["check_exit_code"] is not None or run["check_output_path"]:
            check += (f"; it last ended with exit code {run['check_exit_code']}, "
                      f"its output in {run['check_output_path']}")
        lines.append(check + ".")
    if early is None:
        lines.append(
            "The context started fresh: the handoff above is the last round's, "
            f"and the workspace is as it left it. Each round's handoff is in "
            f"{folder} ({handoff_name} the last). A round ends with end_round."
        )
    else:
        lines.append(
            f"{went_on(early)}; the workspace is as it left it. Each round's "
            f"handoff is in {folder}. A round ends with end_round."
        )
    return "\n".join(lines)
