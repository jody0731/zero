"""The jobs' part in the host (host/parts.py): their rows, their notices,
check-ins, and the `jobs` and `stop_job` verbs.

process.py runs a job's process; this keeps what the session's state needs.
A job's end, a check-in and a loss each reach its session as a `job` message in
the inbox, written in the same transaction as the job's row (store.py), so a
crash can't lose one. Jobs die with the host: a clean stop writes each one's loss before
it kills it, and the boot repair writes it for a crash. Under systemd the
host's unit takes a crashed host's jobs with it; a detached host's are killed
by the next boot repair, by process group, if their pid still has the start
time recorded for it (Codex's check).
"""

import os
import signal
import threading
import time
import traceback
from pathlib import Path

from host.parts import Part, Refused
from jobs import SETTINGS, process
from jobs import tool as jobtools
from jobs.store import SCHEMA, JobStore

# Running jobs before another one asks, for a session whose limits name none
MAX_JOBS = {s.name: s.default for s in SETTINGS}["max_jobs"]


def pid_start(pid):
    """When the process with this pid started (/proc's field 22, in clock
    ticks since boot), or None if there is none."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The command name, field 2, is in brackets and may hold spaces
    return stat.rsplit(")", 1)[1].split()[19]


def kill_leftover(row):
    """Kill a lost job's process group, but only if its pid is still that job."""
    if row["pid_start"] is None or pid_start(row["pid"]) != row["pid_start"]:
        return False
    try:
        # Each job leads its own process group (process.py)
        os.killpg(row["pid"], signal.SIGKILL)
    except OSError:
        return False
    return True


class SessionJobs:
    """What one session's tools get as `jobs`: the host, bound to the session."""

    def __init__(self, host, sid, limit):
        self.host = host
        self.sid = sid
        # Running jobs before another one asks: the session's max_jobs
        self.limit = limit

    def spawn(self, argv, **options):
        return process.SPAWNER.popen(argv, **options)

    def room(self):
        """Whether one more job fits under the cap without asking."""
        return len(self.host.store.jobs(self.sid, running=True)) < self.limit

    def count(self):
        return len(self.host.store.jobs(self.sid, running=True))

    def start(self, proc, command, expect_minutes=None, ran=0.0):
        return self.host.start(self.sid, proc, command, expect_minutes, ran)

    def now(self):
        return self.host.clock()

    def list(self):
        return self.host.store.jobs(self.sid)

    def row(self, id):
        row = self.host.store.job(id)
        return row if row is not None and row["session"] == self.sid else None

    def stop(self, id):
        return self.host.stop_job(id, by="model")

    def estimate(self, id, minutes):
        due = self.host.clock() + minutes * 60
        self.host.store.update_job(id, due=due, checkin=due)

    def look(self, id):
        """Its output since the model's last look; a look that sees the end takes
        the place of the end's notice."""
        row = self.host.store.job(id)
        files = self.host.files(id)
        text, dropped = files.read(row["read"])
        self.host.store.update_job(id, read=files.size)
        if row["state"] != "running":
            self.host.store.withdraw(f"job:{id}:end")
        return text, dropped


class Jobs(Part):
    name = "jobs"
    looking = ("jobs",)
    schema = SCHEMA
    tables = ("jobs",)

    def __init__(self, tick=2.0, clock=time.time):
        """`tick`: how often check-ins are looked for, in seconds."""
        self.tick = tick
        self.clock = clock
        # Jobs running in this process: id → process.Job
        self.live = {}
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.ticker = None

    def attach(self, runtime):
        """Its rows in the runtime's store; `wake()`, the runtime's dispatch
        after a notice is admitted."""
        super().attach(runtime)
        self.store = JobStore(runtime.store)
        self.folder = runtime.paths.jobs
        self.wake = runtime.wake

    def verbs(self):
        return {"jobs": self.jobs_verb, "stop_job": self.stop_job_verb}

    def jobs_verb(self, request):
        """A session's jobs, ended ones too (`zero show`); or every running
        one."""
        sid = request.get("session")
        if sid is not None and not isinstance(sid, str):
            raise Refused("session must be a string")
        return {"ok": True, "jobs": self.rows(sid)}

    def stop_job_verb(self, request):
        job = request.get("job")
        if not isinstance(job, str):
            raise Refused("stop_job needs the job's id, a string")
        return {"ok": True, "stopped": self.stop_job(job, by="user")}

    def rows(self, sid=None):
        """A session's jobs, ended ones too, or every running one; each with
        its state in words (`said`), as the model reads it, so a client needs
        no harness code to say it."""
        rows = self.running() if sid is None else self.store.jobs(sid)
        now = time.time()
        return [{**row, "said": jobtools.state_text(row, now)} for row in rows]

    def session(self, sid, limit):
        return SessionJobs(self, sid, limit)

    def give(self, sid, session):
        """The session's tools start jobs through the host, up to its limit."""
        limit = session.tool_context.get("limits", {}).get("max_jobs")
        session.tool_context["jobs"] = self.session(sid, limit or MAX_JOBS)

    def busy(self):
        return {"jobs": [row["id"] for row in self.running()]}

    def refuse_delete(self, sid):
        if self.store.jobs(sid, running=True):
            return "a job of this session is running; stop it first"
        return None

    def session_files(self, sid):
        """Each job's output."""
        found = []
        for job in self.store.jobs(sid):
            found += sorted(self.folder.glob(f"{job['id']}.*"))
        return found

    def annotate(self, rows):
        """Each session's running jobs, counted."""
        counts = {}
        for job in self.store.jobs(running=True):
            counts[job["session"]] = counts.get(job["session"], 0) + 1
        for row in rows:
            row["jobs"] = counts.get(row["id"], 0)

    def settled(self, sid):
        return {"jobs": [row["id"] for row in self.store.jobs(sid, running=True)]}

    def files(self, id):
        return process.OutputFiles(self.folder / id)

    def last_output(self, row):
        """The output the model hasn't looked at, for a notice."""
        return self.files(row["id"]).read(row["read"])[0]

    def start(self, sid, proc, command, expect_minutes=None, ran=0.0):
        """Record a running process as a job and start its pump. `ran`: how
        long it already ran in the foreground (a command moved at bash's
        timeout), so its clock starts with the command; its estimate and first
        check-in count from now, when it became a job."""
        id = process.new_id()
        now = self.clock()
        due = now + expect_minutes * 60 if expect_minutes else None
        checkin = due if due is not None else now + jobtools.FIRST_CHECKIN
        self.store.add_job(id, sid, command, proc.pid, now - ran, due, checkin,
                           pid_start(proc.pid))
        job = process.Job(id, proc, self.files(id), self.ended)
        with self.lock:
            self.live[id] = job
        job.start()
        return id

    def ended(self, job, code):
        """Runs in the job's thread once its process is gone."""
        with self.lock:
            known = self.live.pop(job.id, None) is not None
        # A clean stop wrote its loss before stopping it; a forgotten job is
        # the boot repair's to report
        if not known or job.stopped_by == "shutdown":
            return
        now = self.clock()
        row = self.store.job(job.id)
        if job.stopped_by == "model":
            # The model stopped it itself, so it already knows
            self.store.settle_job(
                job.id, state="stopped", code=code, ended=now, stopped_by="model"
            )
            return
        state = "stopped" if job.stopped_by else "ended"
        settled = {**row, "state": state, "code": code, "ended": now,
                   "stopped_by": job.stopped_by}
        text = jobtools.ended_notice(settled, self.last_output(row), now)
        self.store.settle_job(
            job.id, notice=(f"job:{job.id}:end", text), state=state, code=code,
            ended=now, stopped_by=job.stopped_by,
        )
        self.wake()

    def stop_job(self, id, by):
        """Stop a running job: True if it was running."""
        with self.lock:
            job = self.live.get(id)
        return job is not None and job.stop(by)

    def running(self):
        return self.store.jobs(running=True)

    def check_ins(self):
        """A notice for each running job past its check-in time; the next one
        comes after as long again as it has run so far."""
        now = self.clock()
        woke = False
        for row in self.running():
            if row["checkin"] > now:
                continue
            count = row["checkins"] + 1
            text = jobtools.checkin_notice(row, self.last_output(row), now)
            notice = (f"job:{row['id']}:checkin:{count}", text)
            following = now + (now - row["started"])
            woke |= self.store.settle_job(
                row["id"], notice=notice, checkins=count, checkin=following
            )
        if woke:
            self.wake()

    def run_ticker(self):
        while not self.stopping.wait(self.tick):
            try:
                self.check_ins()
            except Exception:
                # A failed look is tried again at the next tick
                traceback.print_exc()

    def begin(self):
        """At boot: report jobs a crash cut off, then start looking for check-ins."""
        now = self.clock()
        for row in self.running():
            if row["id"] in self.live:
                continue
            kill_leftover(row)
            text = jobtools.lost_notice(row, self.last_output(row), now, crashed=True)
            self.store.settle_job(
                row["id"], notice=(f"job:{row['id']}:lost", text), state="lost",
                ended=now,
            )
        self.stopping.clear()
        self.ticker = threading.Thread(target=self.run_ticker, name="jobs",
                                       daemon=True)
        self.ticker.start()

    def shutdown(self):
        """A clean stop: each job's loss is written first, then it is stopped."""
        self.stopping.set()
        now = self.clock()
        with self.lock:
            live = list(self.live.values())
        for job in live:
            row = self.store.job(job.id)
            text = jobtools.lost_notice(row, self.last_output(row), now, crashed=False)
            self.store.settle_job(
                job.id, notice=(f"job:{job.id}:lost", text), state="lost", ended=now
            )
            job.stopped_by = "shutdown"
            job.stop_flag.set()
        for job in live:
            job.thread.join(timeout=5)
            if job.proc.poll() is None:
                process.stop_group(job.proc)

    def forget(self):
        """Drop this process's jobs without writing anything: what a crash does
        (tests only)."""
        self.stopping.set()
        with self.lock:
            self.live.clear()
