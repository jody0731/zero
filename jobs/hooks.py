"""The cap on a session's running jobs: past it, starting another one is an
ask, decided as any ask in the mode (bypassPermissions loads no caps); and
the jobs a turn the user interrupted leaves running."""

from core import tools
from core.hooks import Hook

SUFFIXES = {1: "st", 2: "nd", 3: "rd"}
# After the note of a turn the user interrupted: its jobs run on
STILL_RUNNING = (
    "\n[Harness note: these background jobs of this session still run, until "
    "each ends or is stopped: {jobs}.]"
)


def ordinal(n):
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{SUFFIXES.get(n % 10, 'th')}"


class JobLimit(Hook):
    """Marks a background call past the running-jobs cap for the approver."""

    def before_tool(self, call):
        if not tools.starts_job(call.name, call.args):
            return None
        jobs = call.turn.session.tool_context.get("jobs")
        if jobs is None or jobs.has_room():
            return None
        why = str(call.args.get("reason") or "").strip() or "no reason given"
        count = jobs.count()
        call.notes["limit"] = {
            "rule": "job_limit",
            "action": "ask",
            "reason": (
                f"it would start a {ordinal(count + 1)} running job; this "
                f"session's limit is {jobs.limit}; reason: {why.rstrip('.')}"
            ),
            "about": (
                f"It would start a {ordinal(count + 1)} running job, past this "
                f"session's limit of {jobs.limit}."
            ),
        }
        return None


class RunningJobs(Hook):
    """Says which jobs still run when the user interrupts a turn: a job
    outlives its turn, and stopping the turn stops none."""

    def interrupted(self, session):
        jobs = session.tool_context.get("jobs")
        if jobs is None:
            return ""
        running = [row for row in jobs.list() if row["state"] == "running"]
        if not running:
            return ""
        named = ", ".join(f"{row['id']} (`{row['command']}`)" for row in running)
        return STILL_RUNNING.format(jobs=named)
