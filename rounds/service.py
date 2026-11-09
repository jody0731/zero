"""The rounds' service in the server (server/services.py): their rows, the
next round's message, and the `rounds/list` and `rounds/stop` messages.

A round's end (hooks.py) settles the run's row and queues the next round's
message in one transaction (store.py), so a crash can't lose one: a message
in the inbox survives a restart, and an idle session starts a turn on it as
on a job's notice. A round cut off mid-turn resumes as any turn does. Each
round's handoff is kept in the session's results folder, beside the outputs
compaction saved.
"""

import secrets
import time
from pathlib import Path

from server.services import Refused, Service
from rounds import tool as roundtools
from rounds.hooks import FAILED, STOPPED
from rounds.store import SCHEMA, RoundStore

# Rounds in a row that end failed before the run counts as stuck
FAILED_ROUNDS = 2


def new_id():
    return f"rounds-{secrets.token_hex(3)}"


def save(folder, name, text):
    """`text` saved whole as `name` in the session's results folder, readable
    by the user only; its path."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def summary(run):
    """A run in a line, as the status line and `zero rounds list` show it:
    `round 3/8 · 42 min · $0.12 of $1.00`."""
    return (
        f"round {run['round_count']}/{run['budget_rounds']} · "
        f"{roundtools.duration(run['spent_minutes'])} of "
        f"{roundtools.duration(run['budget_minutes'])} · "
        f"{roundtools.dollars(run['spent_dollars'])} of "
        f"{roundtools.dollars(run['budget_dollars'])}"
    )


class SessionRounds:
    """What one session's tools and hooks get as `session_rounds`: the
    server's rounds, bound to the session and its results folder."""

    def __init__(self, service, sid, folder):
        self.service = service
        self.sid = sid
        # The session's results folder, a callable (a new log is named when
        # its first turn starts)
        self.folder = folder

    def open(self):
        return self.service.store.open_run(self.sid)

    def run_folder(self, id):
        """Where run `id` keeps its files: a folder of its own in the
        session's, so a later run's round-1.md can't replace an earlier
        one's."""
        return Path(self.folder()) / id

    def save(self, id, name, text):
        return save(self.run_folder(id), name, text)

    def start(self, objective, check, budget):
        """A new run, this turn its round 1; or, while one is open, its budget
        from now: (its row, whether it was a new budget)."""
        run = self.open()
        rounds, minutes, money = budget
        if run is None:
            id = new_id()
            self.service.store.add(id, self.sid, objective, check, budget, None)
            return self.service.store.run(id), False
        fields = {
            "objective": objective, "state": "running",
            "budget_rounds": run["round_count"] + rounds,
            "budget_minutes": run["spent_minutes"] + minutes,
            "budget_dollars": run["spent_dollars"] + money,
        }
        if check:
            fields["done_check"] = check
        self.service.store.update(run["id"], **fields)
        return self.service.store.run(run["id"]), True

    def next_state(self, run, outcome, end, minutes, money):
        """What a round's end makes of its run: running (the next round),
        waiting (its budget spent), done, stuck or stopped."""
        if outcome in STOPPED:
            return "stopped"
        if outcome in FAILED:
            failed = run["failed_count"] + 1
            return "stuck" if failed >= FAILED_ROUNDS else "running"
        status = end["status"] if end else "going_on"
        if status in ("done", "stuck"):
            return status
        if run["state"] == "waiting":
            # The turn the spent budget opened gave no new one: it stops
            # rather than ask again
            return "stopped"
        spent = (
            run["round_count"] >= run["budget_rounds"]
            or run["spent_minutes"] + minutes >= run["budget_minutes"]
            or run["spent_dollars"] + money >= run["budget_dollars"] > 0
        )
        return "waiting" if spent else "running"

    def finish_round(self, id, state, outcome, end, minutes, money,
                     fresh=True):
        """The round's end written to its row, with the next round's message
        (or the budget spent's) in the same transaction. `fresh`: the next
        round's context starts from the handoff; else (a failed round, a
        handoff that failed) it goes on from the last round's."""
        run = self.service.store.run(id)
        number = run["round_count"]
        fields = {
            "state": state,
            "spent_minutes": run["spent_minutes"] + minutes,
            "spent_dollars": run["spent_dollars"] + money,
            "failed_count": run["failed_count"] + 1 if outcome in FAILED else 0,
        }
        handoff_name = f"round-{number}.md"
        last = None
        if not fresh:
            last = (f"ended early ({outcome})" if outcome in FAILED
                    else "couldn't write its handoff")
        if end is not None:
            fields["last_status"] = end["status"]
            self.save(id, handoff_name, end["handoff"])
            if end.get("check"):
                fields["check_exit_code"] = end["check"]["exit_code"]
                fields["check_output_path"] = end["check"]["path"]
        notice = None
        if state == "running":
            fields["round_count"] = number + 1
            msg_id = f"rounds:{id}:{number + 1}"
            fields["round_inbox"] = msg_id
            text = roundtools.round_text({**run, **fields}, self.run_folder(id),
                                         handoff_name, last)
            notice = (msg_id, text)
        elif state == "waiting":
            msg_id = f"rounds:{id}:spent:{number}"
            fields["round_inbox"] = msg_id
            text = roundtools.spent_text({**run, **fields}, self.run_folder(id),
                                         last)
            notice = (msg_id, text)
        else:
            fields["ended_at"] = self.service.clock()
        self.service.store.update(id, notice=notice, **fields)
        if notice is not None:
            self.service.wake()


class RoundsService(Service):
    name = "rounds"
    looking_messages = ("rounds/list",)
    schema = SCHEMA
    tables = ("rounds",)

    def __init__(self, clock=time.time):
        self.clock = clock

    def attach(self, runtime):
        super().attach(runtime)
        self.store = RoundStore(runtime.store)
        self.wake = runtime.wake

    def messages(self):
        return {"rounds/list": self.list_message, "rounds/stop": self.stop_message}

    def list_message(self, request):
        """A session's runs, ended ones too; or every open one."""
        sid = request.get("session")
        if sid is not None and not isinstance(sid, str):
            raise Refused("session must be a string")
        runs = (self.store.runs(open_only=True) if sid is None
                else self.store.runs(sid))
        return {"ok": True, "rounds": [{**run, "summary": summary(run)}
                                       for run in runs]}

    def stop_message(self, request):
        id = request.get("rounds")
        if not isinstance(id, str):
            raise Refused("rounds/stop needs the run's id, a string")
        return {"ok": True, "stopped": self.stop(id)}

    def stop(self, id):
        """Stop an open run: no next round starts; a round under way finishes
        its turn. True if it was open."""
        run = self.store.run(id)
        if run is None or run["state"] not in ("running", "waiting"):
            return False
        self.store.update(id, state="stopped", ended_at=self.clock())
        if run["round_inbox"]:
            self.store.withdraw(run["round_inbox"])
        return True

    def session_built(self, sid, session):
        folder = session.tool_context.get("results")
        if folder is not None:
            session.tool_context["session_rounds"] = SessionRounds(self, sid, folder)

    def annotate(self, rows):
        """Each session's open run, in a line."""
        found = {run["session"]: summary(run)
                 for run in self.store.runs(open_only=True)}
        for row in rows:
            row["rounds"] = found.get(row["id"])

    def refuse_delete(self, sid):
        if self.store.open_run(sid) is not None:
            return "work in rounds is open in this session; stop it first"
        return None

    def refuse_rewind(self, sid):
        return self.refuse_delete(sid)

    def settled(self, sid):
        run = self.store.open_run(sid)
        return {"rounds": summary(run) if run else None}
