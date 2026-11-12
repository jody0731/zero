"""The bash tool: own process group, timeout, clipped output, Codex-style header.

Where the session has a server for jobs, a call can start in the background,
and a command still running at the timeout moves there instead of being killed
(jobs/).
"""

import itertools
import subprocess
import time

from core.asking import TO_APPROVER
from core.tools import Grant, Result, tool
from jobs import tool as jobtools
from jobs.process import drain, read_output, stop_group
from shell.output import OutputBuffer, output_parts
from sandbox import check as sandbox_check
from sandbox.sandbox import (
    NOT_OUTSIDE, OUTSIDE_ARGUMENT, WALL, WALL_MARKS, outside_crossing,
)
from shell.commands import prefix_words, prefixes

CUT_OUTPUT = (
    " Output after the kill was cut: a process outside its group still held it open."
)
# Where a command runs when nobody hands it a sandbox: none, unless a test
# calling bash() directly sets one. A session hands its own, so sessions side by
# side never share one
SANDBOX = None

NO_BACKGROUND = (
    "(Background jobs aren't available in this session, so it ran in the "
    "foreground.)"
)
NOT_MOVED = (
    " It wasn't moved to the background: this session already runs {count} jobs, "
    "its limit."
)


# What a command's answer carries beside its text (core/tools.py's
# Result), as the screens and the judge read it
FIELDS = {
    "exit_code": ("integer", "The command's exit code, where it exited on its "
                  "own in the foreground"),
    "wall_seconds": ("number", "How long it ran in the foreground, in seconds"),
    "job_id": ("string", "The id of the background job it runs as: started "
               "there, or moved there after the foreground timeout"),
    "timed_out": ("boolean", "Still running at the foreground timeout: moved "
                  "to the background as a job, or killed"),
}


def result_summary(fields):
    """A command that became a job, as the judge's view says it."""
    if not fields.get("job_id"):
        return None
    how = "moved to" if fields.get("timed_out") else "started in"
    return f"{how} the background as {fields['job_id']}"


def describe(limits, context=None):
    """bash's description, with the session's timeout; without a session's
    limits (the registry's own copy), the timeout isn't a number yet."""
    timeout = limits.get("bash_foreground_seconds")
    after = f"{timeout:g} seconds" if timeout else "the session's timeout"
    return (
        "Run a bash command and return its exit code and output (stdout and "
        "stderr). Each call is a fresh shell in the workspace folder: cd and "
        f"variables do not carry over. A command still running after {after} "
        "moves to the background as a job, where the session has jobs, or is "
        "killed."
    )


@tool(
    describe,
    optional=(OUTSIDE_ARGUMENT, "reason", "background", "expect_minutes"),
    summary=lambda args: args["command"],
    # A call with `background` starts a job (the jobs' cap)
    starts_job="background",
    fields=FIELDS,
    result_summary=result_summary,
    crossing=outside_crossing,
    # "Don't ask again" grants each part's prefix (shell/commands.py)
    grant=Grant("prefixes", lambda args: prefixes(str(args.get("command", ""))),
                prefix_words),
    command=("string", "The bash command to run"),
    **{OUTSIDE_ARGUMENT: (
        "boolean",
        "Run this one command outside the sandbox, as the user with the "
        f"network. It goes first to {TO_APPROVER}",
    )},
    reason=("string", f"With {OUTSIDE_ARGUMENT}, or for a job past the session's "
            "limit: why the task needs it"),
    background=(
        "boolean",
        "Run it as a background job: the call returns at once with the job's id, "
        "and the job runs side by side with this turn and with other jobs, "
        "with no call to your model while it runs. A notice comes when it "
        "ends: while a turn is running, at its next step; once the turn has "
        "ended, the notice starts a new one",
    ),
    expect_minutes=(
        "number", "With background: roughly how many minutes it should take",
    ),
)
def bash(
    command, outside_sandbox=False, reason="", background=False,
    expect_minutes=None, sandbox=None, outside=None, stop=None, jobs=None,
    limits=None,
):
    """`sandbox`, `outside`, `stop`, `jobs` and `limits` come from the session,
    never the model. `outside`: where an approved call to leave the sandbox
    runs; None when this session can't leave it. `jobs`: the session's server
    for background jobs; None when it has none. `limits`: its timeout and the
    output it keeps, as they stand (every session's tools get them).
    `reason` is for the ask (permissions/asks.py, jobs/hooks.py)."""
    box = sandbox or SANDBOX
    timeout = limits["bash_foreground_seconds"]
    note = ""
    if outside_sandbox is True:
        if outside is not None:
            box = outside
        elif box.sandboxed:
            note = NOT_OUTSIDE
    if background is True and jobs is None:
        note = f"{note}\n{NO_BACKGROUND}" if note else NO_BACKGROUND
    spawn = jobs.spawn if jobs is not None else subprocess.Popen
    # A start the sandbox spoiled ran nothing, so it may go again, a fresh
    # look each time (start_again); a background job starts once
    for tries in itertools.count(1):
        t0 = time.monotonic()
        # Own process group, so a timeout can kill everything bash started; with a
        # server, started by its spawner, so a command can outlive this thread.
        # The sandbox raises rather than hand back a bare command (fail closed)
        proc = spawn(
            box.command(["bash", "-c", command]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=box.env(),
            cwd=box.workspace,
            start_new_session=True,
        )
        if background is True and jobs is not None:
            # A number of minutes, or no estimate (true is an int in Python)
            valid = isinstance(expect_minutes, (int, float)) and not isinstance(
                expect_minutes, bool
            )
            estimate = expect_minutes if valid and expect_minutes > 0 else None
            job_id = jobs.start(proc, command, estimate)
            result = jobtools.started_text(job_id)
            return Result(result + "\n\n" + note if note else result,
                          fields={"job_id": job_id})
        output = OutputBuffer(limits["bash_output_kept_chars"])
        moved = False
        fields = {}
        try:
            try:
                read_output(proc, output, timeout=timeout, stop=stop)
            except subprocess.TimeoutExpired:
                if jobs is not None and jobs.has_room():
                    # The rest of its output goes to the job; the pipe stays open
                    moved = True
                    job_id = jobs.start(proc, command, None, time.monotonic() - t0)
                    status = jobtools.moved_status(job_id, timeout)
                    fields = {"job_id": job_id, "timed_out": True}
                else:
                    stop_group(proc)
                    status = f"Timed out after {timeout:g} s and was killed."
                    fields = {"timed_out": True}
                    if drain(proc, output):
                        status += CUT_OUTPUT
                    if jobs is not None:
                        status += NOT_MOVED.format(count=jobs.limit)
            else:
                status = f"Exit code: {proc.returncode}"
                fields = {"exit_code": proc.returncode}
        except KeyboardInterrupt as error:
            # Ctrl+C doesn't reach our own process group, so end it here
            stop_group(proc)
            if drain(proc, output):
                error.output_detail = CUT_OUTPUT
            raise
        finally:
            if not moved:
                proc.stdout.close()
        if moved or not box.start_again(tries, proc.returncode, output.head):
            break
    if moved:
        output.add(b"", final=True)
    seconds = time.monotonic() - t0
    head, result = output_parts(status, seconds, output)
    fields["wall_seconds"] = round(seconds, 1)
    if (box.sandboxed and not moved
            and sandbox_check.failed_to_start(proc.returncode, output.head)):
        why = sandbox_check.why_it_cant_start()
        if why:
            cant = sandbox_check.CANT_START.format(why=why)
            note = f"{note}\n{cant}" if note else cant
    if box.sandboxed and any(mark in result for mark in WALL_MARKS):
        wall = WALL.format(walls=box.walls())
        note = f"{note}\n{wall}" if note else wall
    if note:
        result = result + "\n\n" + note if result else "\n\n" + note
    # Moved to the background, it goes on: not a failure
    failed = not moved and fields.get("exit_code") != 0
    return Result(result, head=head, fields=fields, failed=failed)


# What this component offers the model, in the order it reads them; the
# assembly adds them to core's registry (core/tools.py, add)
TOOLS = (bash,)
