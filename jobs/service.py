"""The jobs' service in the server (server/services.py): their rows, their notices,
check-ins, and the `job/list` and `job/stop` messages.

process.py runs a job's process; this keeps what the session's state needs.
A job's end, a check-in and a loss each reach its session as a `job` message in
the inbox, written in the same transaction as the job's row (store.py), so a
crash can't lose one. Jobs die with the server: a clean stop writes each one's
loss before it kills it, and the boot repair writes it for a crash. Under
systemd the server's unit takes a crashed server's jobs with it; a detached
server's are killed by the next boot repair, by process group, if their pid
still has the start time recorded for it (Codex's check).
"""

import os
import signal
import threading
import time
import traceback
from pathlib import Path

from server.services import Service, Refused
from jobs import process
from jobs import tool as jobtools
from jobs.store import ADDED_COLUMNS, RENAMED_COLUMNS, SCHEMA, JobStore
from protocol.messages import SESSION_ID_ENV

# The sessions' commands kept for command_session before those whose leader
# has gone are let go (design rule 12: nothing grows without a cap)
KEPT_COMMANDS = 1000


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
    """What one session's tools get as `jobs`: the server, bound to the session."""

    def __init__(self, service, sid, limits, log_of=None):
        self.service = service
        self.sid = sid
        # The session's limits as they stand (settings.LiveSettings)
        self.limits = limits
        # The session's log as it stands: a job keeps the record it was
        # started from (the call's), its newest
        self.log_of = log_of

    def spawn(self, argv, **options):
        """Start one of the session's commands, foreground or a job: its
        environment carries the session's id (SESSION_ID_ENV), which a
        workflow's zero names; one that leads a Unix session of its own is
        kept as the session's command, so the server can tell whose a peer is
        (command_session)."""
        env = dict(options.get("env") or os.environ)
        env[SESSION_ID_ENV] = self.sid
        options["env"] = env
        proc = process.SPAWNER.popen(argv, **options)
        if options.get("start_new_session"):
            self.service.command_started(proc.pid, self.sid)
        return proc

    @property
    def limit(self):
        """Running jobs before another one asks: the session's max_jobs as
        it stands."""
        return self.limits["max_jobs"]

    def has_room(self):
        """Whether one more job fits under the cap without asking."""
        return len(self.service.store.jobs(self.sid, running=True)) < self.limit

    def count(self):
        return len(self.service.store.jobs(self.sid, running=True))

    def start(self, proc, command, expect_minutes=None, ran=0.0):
        log = self.log_of() if self.log_of is not None else None
        started_from_seq = log.last_seq if log is not None else None
        return self.service.start(self.sid, proc, command, expect_minutes, ran,
                                  started_from_seq)

    def now(self):
        return self.service.clock()

    def list(self):
        return self.service.store.jobs(self.sid)

    def row(self, id):
        row = self.service.store.job(id)
        return row if row is not None and row["session"] == self.sid else None

    def stop(self, id):
        return self.service.stop_job(id, by="model")

    def set_estimate(self, id, minutes):
        due_at = self.service.clock() + minutes * 60
        self.service.store.update_job(id, due_at=due_at, next_checkin_at=due_at)

    def look(self, id):
        """Its output since the model's last look; a look that sees the end takes
        the place of the end's notice."""
        row = self.service.store.job(id)
        files = self.service.files(id)
        text, dropped, through = files.read_through(row["read_bytes"])
        self.service.store.update_job(id, read_bytes=through)
        if row["state"] != "running":
            self.service.store.withdraw(f"job:{id}:end")
        return text, dropped


class JobsService(Service):
    name = "jobs"
    looking_messages = ("job/list",)
    schema = SCHEMA
    renamed_columns = RENAMED_COLUMNS
    added_columns = ADDED_COLUMNS
    tables = ("jobs",)

    def __init__(self, tick=2.0, clock=time.time):
        """`tick`: how often check-ins are looked for, in seconds."""
        self.tick = tick
        self.clock = clock
        # Jobs running in this process: id → process.Job
        self.live = {}
        self.lock = threading.Lock()
        # Every command a session started that leads a Unix session of its
        # own (each command does, jobs or not): its pid → (its start, the
        # session), for command_session
        self.commands = {}
        self.commands_lock = threading.Lock()
        self.stopping = threading.Event()
        self.ticker = None

    def attach(self, runtime):
        """Its rows in the runtime's store; `wake()`, the runtime's dispatch
        after a notice is admitted."""
        super().attach(runtime)
        self.store = JobStore(runtime.store)
        self.folder = runtime.paths.jobs
        self.wake = runtime.wake

    def messages(self):
        return {"job/list": self.list_message, "job/stop": self.stop_message}

    def command_started(self, pid, sid):
        """Keep a session's command, which leads Unix session `pid`; past
        KEPT_COMMANDS, those whose leader has gone are let go."""
        started = pid_start(pid)
        with self.commands_lock:
            kept = list(self.commands) if len(self.commands) >= KEPT_COMMANDS else []
        gone = [old for old in kept if pid_start(old) is None]
        with self.commands_lock:
            for old in gone:
                self.commands.pop(old, None)
            self.commands[pid] = (started, sid)

    def command_session(self, unix_session):
        """The session whose command leads `unix_session`, or None. A leader
        gone still names it: its pid stays taken while a process of its Unix
        session lives, and the asking peer is one. A leader whose pid now
        has another start is another process."""
        with self.commands_lock:
            found = self.commands.get(unix_session)
        if found is None:
            return None
        started, sid = found
        now = pid_start(unix_session)
        if now is not None and now != started:
            return None
        return sid

    def list_message(self, request):
        """A session's jobs, ended ones too; or every running
        one."""
        sid = request.get("session")
        if sid is not None and not isinstance(sid, str):
            raise Refused("session must be a string")
        return {"ok": True, "jobs": self.rows(sid)}

    def stop_message(self, request):
        job = request.get("job")
        if not isinstance(job, str):
            raise Refused("job/stop needs the job's id, a string")
        return {"ok": True, "stopped": self.stop_job(job, by="user")}

    def rows(self, sid=None):
        """A session's jobs, ended ones too, or every running one; each with
        its state in words (`state_text`), as the model reads it, so a client needs
        no harness code to say it."""
        rows = self.running_jobs() if sid is None else self.store.jobs(sid)
        now = time.time()
        return [listed_row(row, now) for row in rows]

    def session(self, sid, limits, log_of=None):
        return SessionJobs(self, sid, limits, log_of)

    def session_built(self, sid, session):
        """The session's tools start jobs through the server, up to its limit
        as it stands."""
        session.tool_context["jobs"] = self.session(
            sid, session.tool_context["limits"], lambda: session.log)

    def busy_with(self):
        return {"jobs": [row["id"] for row in self.running_jobs()]}

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
        return self.files(row["id"]).read(row["read_bytes"])[0]

    def start(self, sid, proc, command, expect_minutes=None, ran=0.0,
              started_from_seq=None):
        """Record a running process as a job and start its pump. `ran`: how
        long it already ran in the foreground (a command moved at bash's
        timeout), so its clock starts with the command; its estimate and first
        check-in count from now, when it became a job."""
        id = process.new_id()
        now = self.clock()
        due_at = now + expect_minutes * 60 if expect_minutes else None
        next_checkin_at = (due_at if due_at is not None
                           else now + jobtools.FIRST_CHECKIN)
        self.store.add_job(id, sid, command, proc.pid, now - ran, due_at,
                           next_checkin_at, pid_start(proc.pid), started_from_seq)
        job = process.Job(id, proc, self.files(id), self.ended)
        with self.lock:
            self.live[id] = job
        job.start()
        return id

    def ended(self, job, code):
        """Runs in the job's thread once its process is gone. Its end is
        settled whatever fails here: else the row stays running for a job
        that is gone, and keeps the server up until the next start."""
        try:
            self.settle_end(job, code)
        except Exception:
            traceback.print_exc()
            try:
                self.store.settle_job(job.id, state="lost", exit_code=code,
                                      ended_at=self.clock())
            except Exception:
                # The store itself failed: the next start's repair reports it
                traceback.print_exc()

    def settle_end(self, job, code):
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
                job.id, state="stopped", exit_code=code, ended_at=now,
                stopped_by="model",
            )
            return
        state = "stopped" if job.stopped_by else "ended"
        settled = {**row, "state": state, "exit_code": code, "ended_at": now,
                   "stopped_by": job.stopped_by}
        text, summary = self.end_notice(settled, row, now)
        self.store.settle_job(
            job.id, notice=(f"job:{job.id}:end", text, summary), state=state,
            exit_code=code, ended_at=now, stopped_by=job.stopped_by,
        )
        self.wake()

    def end_notice(self, settled, row, now):
        """A job's end as its notice says it, (text, summary): with the
        output the model hasn't looked at, or none if that can't be read."""
        try:
            last = self.last_output(row)
        except OSError:
            traceback.print_exc()
            last = ""
        return jobtools.ended_notice(settled, last, now)

    def stop_job(self, id, by):
        """Stop a running job: True if it was running."""
        with self.lock:
            job = self.live.get(id)
        return job is not None and job.stop(by)

    def running_jobs(self):
        return self.store.jobs(running=True)

    def check_ins(self):
        """A notice for each running job past its check-in time; the next one
        comes after as long again as it has run so far."""
        now = self.clock()
        woke = False
        for row in self.running_jobs():
            if row["next_checkin_at"] > now:
                continue
            count = row["checkin_count"] + 1
            text, summary = jobtools.checkin_notice(row, self.last_output(row),
                                                    now)
            notice = (f"job:{row['id']}:checkin:{count}", text, summary)
            following = now + (now - row["started_at"])
            woke |= self.store.settle_job(
                row["id"], notice=notice, checkin_count=count,
                next_checkin_at=following,
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
        for row in self.running_jobs():
            if row["id"] in self.live:
                continue
            kill_leftover(row)
            text, summary = jobtools.lost_notice(row, self.last_output(row), now,
                                                 crashed=True)
            self.store.settle_job(
                row["id"], notice=(f"job:{row['id']}:lost", text, summary),
                state="lost",
                ended_at=now,
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
            text, summary = jobtools.lost_notice(row, self.last_output(row), now,
                                                 crashed=False)
            self.store.settle_job(
                job.id, notice=(f"job:{job.id}:lost", text, summary),
                state="lost",
                ended_at=now,
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


# A job's row as job/list gives it (protocol/messages.py's row.job): its
# fields by the jobs table's columns they come from, so a column renamed
# there changes no reply
JOB_COLUMNS = ("id", "session", "command", "pid", "state", "exit_code",
               "started_at", "ended_at", "due_at", "stopped_by")


def listed_row(row, now):
    """A job, from its row in the jobs table, with its state in words."""
    return {**{name: row[name] for name in JOB_COLUMNS},
            "state_text": jobtools.state_text(row, now)}
