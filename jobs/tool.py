"""The `job` tool, and the texts the model reads of a job: its start, its
move to the background, its notices.

A job owns its process (process.py). What a job is in the session's state (its
row, its notices, its check-ins) belongs to the server that hands a session its
`jobs` (service.py); without one there are no jobs, and the shell runs
everything in the foreground.
"""

from core import tools
from core.tools import tool

# A job's messages to its session: notices, not the user's words; the
# system message names who sends them (the assembly adds them to core's
# history.NOTICES)
SOURCE = "job"
NOTICE_SENDERS = {SOURCE: "a background job"}

# A job with no estimate checks in first after this, then each time it has run
# as long again
FIRST_CHECKIN = 30 * 60

NOT_HERE = "Background jobs aren't available in this session."


def minutes(seconds):
    if seconds < 60:
        return f"{int(seconds)} s"
    # Rounded, so an estimate of 10 minutes just begun reads as 10, not 9
    whole = round(seconds / 60)
    if whole < 120:
        return f"{whole} min"
    return f"{whole // 60} h {whole % 60} min"


# The notice outlives the turn: without this fact the model slept in its turn
# until the notice came. Both halves, in the words a subagent's start uses
ENDS = (
    "A notice comes when it ends: while a turn is running, at its next step; "
    "once the turn has ended, the notice starts a new one."
)


def started_text(id):
    return f"Started in the background as {id}. {ENDS}"


def moved_status(id, timeout):
    return (
        f"Still running after {timeout:g} s; moved to the background as {id}. "
        f"{ENDS}"
    )


def state_text(row, now):
    if row["state"] == "running":
        text = f"running {minutes(now - row['started_at'])}"
        if row["due_at"] is not None:
            left = row["due_at"] - now
            if left >= 0:
                text += f", expected to end in about {minutes(left)}"
            else:
                text += f", {minutes(-left)} past its estimate"
        return text
    took = minutes((row["ended_at"] or now) - row["started_at"])
    if row["state"] == "ended":
        return f"ended: exit code {row['exit_code']} after {took}"
    if row["state"] == "stopped":
        return f"stopped by the {row['stopped_by']} after {took}"
    return f"lost when ZERO stopped, after {took}"


def tail(text):
    """A notice's output: all that is unread, whole; past its share, the
    context component saves it to a file (context/hooks.py)."""
    return text.strip("\n")


def with_output(summary, last):
    """A notice: its summary, then the output since the model last looked."""
    return f"{summary} Last output:\n{tail(last) or '(none)'}"


def ended_notice(row, last, now):
    """A job's end: (the notice, its summary without the output)."""
    summary = (f"Background job {row['id']} {state_text(row, now)}. Command: "
               f"`{row['command']}`.")
    return with_output(summary, last), summary


def lost_notice(row, last, now, crashed):
    how = (
        "when ZERO or the machine went down" if crashed
        else "when ZERO was stopped"
    )
    ran = minutes(now - row["started_at"])
    summary = (
        f"Background job {row['id']} was cut off {how}, after running "
        f"{'at least ' if crashed else ''}{ran}. Its outcome is unknown. "
        f"Command: `{row['command']}`."
    )
    return with_output(summary, last), summary


def checkin_notice(row, last, now):
    ran = minutes(now - row["started_at"])
    if row["due_at"] is None:
        where = f"has run {ran} with no estimate"
    else:
        where = f"has run {ran} and is past its estimate"
    summary = (
        f"Check-in: background job {row['id']} {where}; it is still running. "
        f"Command: `{row['command']}`."
    )
    return with_output(summary, last), summary


def job_summary(args):
    """A call as an ask and the judge show it (on the raw arguments a stop of
    the model's own job looked like any job's)."""
    if args.get("id") is None:
        return "job: list this session's jobs"
    if args.get("stop") is True:
        return f"job {args['id']}: stop"
    text = f"job {args['id']}: look"
    if args.get("expect_minutes") is not None:
        text += f", new estimate {args['expect_minutes']:g} min"
    return text


# What a background job is for, beside the other means in the system message
# (prompt/instructions.py, MEANS_FACTS); how one starts, by the tool and
# argument that start it, as that tool declares them (core/tools.py's
# job_starter)
MEANS = (
    "a background job{started}: a command that takes long. It runs with no "
    "call to your model while you go on."
)
MEANS_STARTED = " ({tool}'s `{argument}`)"
DESCRIPTION = (
    "Background jobs{started}, or moved there after its timeout. With no id: "
    "list this session's jobs. With an id: its state and its output since you "
    "last looked. `stop`: stop it. `expect_minutes`: a new estimate of how long "
    "it still needs, from now."
)
STARTED = " started with {tool}'s `{argument}`"


def started_with(text, context=None):
    """`text` ({started}) with the tool and argument that start a job, as
    that tool declares them; nothing where no tool offered starts one."""
    starter = tools.job_starter(context)
    return "" if starter is None else text.format(tool=starter[0],
                                                  argument=starter[1])


def means(context):
    """What a background job is for, where the session has a server for
    jobs; None where it hasn't."""
    if context.get("jobs") is None:
        return None
    return MEANS.format(started=started_with(MEANS_STARTED, context))


def describe(limits, context=None):
    """The job tool's description, naming what starts a job."""
    return DESCRIPTION.format(started=started_with(STARTED, context))


@tool(
    describe,
    optional=("id", "stop", "expect_minutes"),
    summary=job_summary,
    # A running job's output: the same call looks again
    changing=True,
    # Only a stop changes anything; a new estimate is the job's own note
    needs_permission=lambda args: args.get("stop") is True,
    # A means only where the session has a server for jobs
    means=means,
    id=("string", "The job's id, e.g. job-3f9a2c"),
    stop=("boolean", "Stop the job"),
    expect_minutes=("number", "A new estimate, in minutes from now"),
)
def job(id=None, stop=False, expect_minutes=None, jobs=None):
    """`jobs`: the session's jobs, from its server; None means there are none.
    The output since the last look comes whole: past its share, the context
    component saves it to a file. `stop` is the model's argument: the turn's stop flag,
    which the session hands every tool that names it, skips a name the tool's
    arguments hold (core/tools.py, run)."""
    if jobs is None:
        return NOT_HERE
    if id is None:
        rows = jobs.list()
        if not rows:
            return "No background jobs in this session."
        now = jobs.now()
        return "\n".join(
            f"{r['id']}  {state_text(r, now)}  {r['command']}" for r in rows
        )
    row = jobs.row(id)
    if row is None and not str(id).startswith("job-"):
        # The id as a model may write it, without its prefix
        id = f"job-{id}"
        row = jobs.row(id)
    if row is None:
        return f"Unknown job {id}."
    if stop is True:
        if jobs.stop(id):
            return f"Stopped {id}: its process group was sent SIGTERM, then SIGKILL."
        return f"{id} wasn't running: {state_text(row, jobs.now())}."
    lines = []
    if expect_minutes is not None:
        if not isinstance(expect_minutes, (int, float)) or expect_minutes <= 0:
            return "Error: expect_minutes must be a positive number."
        jobs.set_estimate(id, float(expect_minutes))
        lines.append(f"New estimate: about {minutes(expect_minutes * 60)} from now.")
        row = jobs.row(id)
    text, dropped = jobs.look(id)
    lines.append(f"{id}: {state_text(row, jobs.now())}. Command: `{row['command']}`")
    if dropped:
        lines.append(
            f"[{dropped} bytes of its output were not kept.]"
        )
    lines.append("Output since your last look:")
    lines.append(text if text else "(no new output)")
    return "\n".join(lines)


# What this component offers the model, in the order it reads them; the
# assembly adds them to core's registry (core/tools.py, add)
TOOLS = (job,)
