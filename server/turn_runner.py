"""Turn execution, resumption, and recovery from the session log."""

import json
import threading
import time
import traceback
from pathlib import Path

from core import attribution, entries, replay, timeline
from core.asking import NO, PARK
from core.control import Control
from core.loop import INTERRUPTED, Session
from server import rebuild
from server.admission import picked
from server.rebuild import FAILED
from server.services import Refused
from server.session_actions import title
from sessions import upgrade
from sessions.lock import Busy
from sessions.log import fresh_log

MAX_RESUMES = 3


def _payload_entries(payload):
    """Reconstruct child entries (e.g. attachments/file) from an inbox payload."""
    raw = payload.get("entries") if isinstance(payload, dict) else None
    if not raw:
        return ()
    return tuple(entries.Entry.from_dict(item, root=False) for item in raw)


def switch_note(changes, session):
    """What the model reads when the user changed the session between turns:
    what changed, and what the hooks say it runs with now (the `switched`
    hook: Environment's workspace and sandbox)."""
    said = "; ".join(f"{name} {old} → {new}" for name, (old, new) in changes.items())
    now = session.hooks.collect("switched", session)
    return f"[Harness note: the user changed this session's {said}. From here on:{now}]"


def turn_records(log):
    """The current branch and open turn, with whether the next writer upgrades."""
    try:
        records = log.branch()
    except timeline.OldLog:
        records = timeline.branch(upgrade.read_records(log))
        return records, timeline.unfinished_turn(records), True
    old = timeline.log_format(records) != timeline.LOG_FORMAT and log.path.exists()
    return records, log.unfinished_turn(), old


class TurnRunner:
    def __init__(self, runtime):
        self.runtime = runtime

    def adopt_logs(self):
        """Design rule 9: a log with no row (its database lost or replaced,
        a fork cut off between its log and its row, a log copied in) is
        listed again, its row made from its header (rebuild.adopted_row),
        its other rows then rebuilt from it as any log ahead of its rows
        is. A file that isn't a session's log is left alone."""
        runtime = self.runtime
        known = runtime.store.session_ids()
        for path in sorted(runtime.paths.sessions.glob("*.jsonl")):
            sid = path.stem
            # An upgraded log's original beside it starts as a log does
            if sid in known or upgrade.is_backup(path):
                continue
            try:
                log = runtime.log(sid)
                header = upgrade.read_header(log)
                if header is None or not entries.has(header.entry, "header"):
                    # Not a session's log
                    continue
                records = upgrade.read_records(log)
                row = rebuild.adopted_row(sid, records, title)
                if row is None:
                    continue
                runtime.store.add_session(sid, row)
                print(f"ZERO's server: listed {sid} again from its log "
                      "(it had no row)", flush=True)
            except Exception:
                traceback.print_exc()

    def rebuild_ahead(self):
        """Design rule 9: rebuild the rows of each session whose log is
        ahead of them (past its row's `log_seq`), from the log, under the
        session's lock: the kernel's (server/rebuild.py) and each service's,
        in one transaction. A session in step is only looked at: its log's
        last record, read from the end. A log with no row is adopted first."""
        runtime = self.runtime
        runtime.adopt_logs()
        services = [service.rebuild for service in runtime.services.values()]
        missing = {row["id"] for row in runtime.store.query(
            "SELECT id FROM sessions WHERE state IN ('running', 'waiting') "
            "AND NOT EXISTS (SELECT 1 FROM inbox WHERE session = sessions.id "
            "AND status = 'delivered')"
        )}
        for row in runtime.store.sessions():
            sid = row["id"]
            log = runtime.log(sid)
            try:
                last = upgrade.last_seq(log)
            except ValueError:
                # A broken log: its next turn records the failure
                traceback.print_exc()
                continue
            if not last or (sid not in missing and last <= row["log_seq"]):
                continue
            lock = runtime.session_lock(sid)
            try:
                lock.acquire()
            except Busy as error:
                runtime.state(sid).blocked = True
                print(f"Skipping {sid}: {error}")
                continue
            try:
                records = upgrade.read_records(log)
                runtime.store.rebuild(
                    sid, rebuild.kernel_rows(records), records, services,
                )
            except Exception:
                traceback.print_exc()
            finally:
                lock.release()

    def repair(self, row, lock=None):
        runtime = self.runtime
        own = lock is None
        lock = lock or runtime.session_lock(row["session"])
        if own:
            lock.acquire()
        try:
            runtime._repair(row, lock)
        finally:
            if own:
                lock.release()

    def _repair(self, row, lock):
        runtime = self.runtime
        sid = row["session"]
        current = runtime.store.query("SELECT * FROM inbox WHERE id = ?", row["id"])[0]
        if current["status"] == "done":
            return
        row = current
        log = runtime.log(sid, lock)
        try:
            all_records, pending, old = turn_records(log)
            records = [r.entry for r in all_records
                       if r.entry.get("inbox") == row["id"]]
            end = next((entry for entry in reversed(records)
                        if entries.has(entry, "turn_end")), None)
            outcome = end.get("outcome") if end is not None else None
        except timeline.NewerLog:
            runtime.state(sid).blocked = True
            return
        except Exception:
            upgrade.record_failure(log, row["id"], traceback.format_exc())
            runtime.store.finish(row["arrival"], sid, "failed")
            return
        # A reply to an ask already in the log: the ask closed with it, if the
        # server died before its row was
        answer = next((entry for entry in reversed(records)
                       if entries.has(entry, "answer")), None)
        open_ask = runtime.store.open_ask(sid) if answer is not None else None
        if open_ask and open_ask["id"] == entries.linked_to(answer, "answers"):
            runtime.store.close_ask(open_ask["id"], row["id"],
                                    bool(answer.get("yes")), row["source"])
            return
        # A steer, or a reply to an ask, already in the log
        if answer is not None or any(entry.get("steer") for entry in records):
            runtime.store.mark_steer_done(row["id"])
            return
        if row["kind"] == "stop":
            if not pending:
                runtime.store.mark_steer_done(row["id"])
            return
        ask = timeline.parked_ask(pending, current_turn=True)
        if ask is not None and pending and pending[0].entry.get("inbox") == row["id"]:
            # Parked: it waits for its reply, with no resume and no budget used
            runtime.store.park(sid, ask, row["id"])
            return
        if end is not None:
            # It ended; only the row's update was lost (and the services'
            # word of it, which they keep once)
            failed = outcome in FAILED
            state = "failed" if failed else "idle"
            runtime.store.finish(row["arrival"], sid, state)
            runtime.turn_ended(sid, outcome)
        elif records:
            stops = runtime.store.stops(sid, row)
            if stops:
                try:
                    if old:
                        runtime.upgrade_session(sid, log)
                        pending = log.unfinished_turn()
                except Exception:
                    upgrade.record_failure(log, row["id"], traceback.format_exc())
                    runtime.store.finish(row["arrival"], sid, "failed")
                    for stop in stops:
                        runtime.store.mark_steer_done(stop["id"])
                    return
                runtime.store.withdraw_messages(
                    sid, row["arrival"], stops[-1]["arrival"],
                )
                runtime.repair_end(log, pending, row, "interrupted",
                                "stopped before a restart", stopped=True)
                for stop in stops:
                    runtime.store.mark_steer_done(stop["id"])
                runtime.store.finish(row["arrival"], sid, "idle")
                runtime.turn_ended(sid, "interrupted")
            elif row["clean_stop"]:
                # A clean stop cut it off: it resumes without using a try
                runtime.store.resume(row["arrival"], sid, counted=False)
            elif row["resume_count"] < MAX_RESUMES:
                runtime.store.resume(row["arrival"], sid)
            else:
                runtime.repair_end(
                    log, pending, row, "failed", "cut off after 3 resumes",
                )
                runtime.store.finish(row["arrival"], sid, "failed")
                runtime.turn_ended(sid, "failed")
        elif row["status"] == "delivered":
            # Marked begun with no record of it: nothing ran, so it can run
            runtime.store.requeue(row["arrival"], sid)

    def repair_end(self, log, pending, row, outcome, detail, stopped=False):
        """Close a cut-off turn without calling either model."""
        runtime = self.runtime
        found = upgrade.file_format(log.path)
        if found < timeline.LOG_FORMAT and log.path.exists():
            upgrade.upgrade_file(log, None)
            pending = log.unfinished_turn()
            found = timeline.LOG_FORMAT
        if found == timeline.LOG_FORMAT:
            writer = runtime.state(row["session"]).harness
            if writer is None:
                writer = Session(None, [], "", log, {})
            else:
                writer.restore(log)
            write = writer.write
        else:
            write = log.append
        for fields in timeline.closing_results(pending, replay.UNKNOWN, "unknown"):
            write("tool", **fields)
        if stopped:
            write("note", kind="interrupted", text=INTERRUPTED)
            for steer in runtime.store.steers(
                row["session"], row["arrival"], notices_only=True,
            ):
                write("user", **attribution.steer_fields(steer))
                runtime.store.mark_steer_done(steer["inbox"])
        write("turn_end", inbox=row["id"], outcome=outcome, detail=detail,
              seconds=0.0, agent_seconds=0.0, wait_seconds=0.0)

    def dispatch(self, sid=None):
        """Start a runner for the session `sid`, if a message of it can
        start a turn and none runs; for every such session when `sid` is
        None (the start, a service's notice)."""
        runtime = self.runtime
        if not runtime.accepting:
            return
        for each in ([sid] if sid is not None else runtime.store.waiting_sessions()):
            runtime.start_runner(each)

    def start_runner(self, sid):
        runtime = self.runtime
        state = runtime.states.get(sid)
        if state is None:
            # Avoid recreating state for a deleted session.
            if not runtime.store.has_queued(sid):
                return
            state = runtime.state(sid)
        with state.lock:
            if (state.runner is not None or state.blocked
                    or not runtime.store.has_queued(sid)):
                return
            thread = threading.Thread(
                target=runtime.runner, args=(sid, state), name=f"session-{sid}",
                # A stop doesn't wait on a thread past the drain
                daemon=True,
            )
            # The idle close come first: the message waits for the next start
            if runtime.hold_runner(state, thread, while_accepting=True):
                thread.start()

    def runner(self, sid, state):
        """Run a session's queued messages one at a time, then leave."""
        runtime = self.runtime
        drained = False
        lock = runtime.session_lock(sid)
        try:
            lock.acquire()
        except Busy as error:
            print(f"Skipping {sid}: {error}")
            with state.lock:
                state.blocked = True
                runtime.let_go(state, leaving=False)
            return
        try:
            drained = runtime.run_queued(sid, state, lock)
        finally:
            with state.lock:
                lock.release()
                if not drained:
                    # Left to the next start's repair: its queued message no
                    # longer keeps this server up
                    state.blocked = True
                # Past its last turn, the runner still has its last word to
                # say: `settled`, then a dispatch that starts a turn on a
                # message admitted meanwhile
                runtime.let_go(state, leaving=True)
        try:
            if drained:
                settled = runtime.settled(sid)
                if settled is not None:
                    runtime.publish(sid, {"settled": settled})
                runtime.dispatch(sid)
        finally:
            runtime.said_last_word(state)

    def run_queued(self, sid, state, lock):
        runtime = self.runtime
        while True:
            # Checking for work and leaving are one step, so a message admitted
            # meanwhile either is seen here or finds no runner and starts one
            with state.lock:
                row = runtime.store.next_queued(sid) if runtime.accepting else None
                if row is None:
                    return True
            try:
                runtime.run_turn(sid, row, lock=lock)
            except Exception:
                # The log or the database failed, not the turn: leave this session
                # to the next start's repair rather than loop on it
                traceback.print_exc()
                return False

    def run_turn(self, sid, row, lock=None):
        runtime = self.runtime
        own = lock is None
        lock = lock or runtime.session_lock(sid)
        if own:
            lock.acquire()
        state = runtime.state(sid)

        def deliver(steer, write):
            with state.lock:
                if control.stop.is_set() and not attribution.is_notice(steer):
                    return
                write()
                runtime.store.mark_steer_done(steer["inbox"])

        control = Control(
            steers=lambda: (
                runtime.store.steers(
                    sid, row["arrival"], notices_only=control.stop.is_set()
                ) if runtime.accepting else []
            ),
            deliver=deliver,
            draft=lambda step, draft: runtime.draft(sid, step, draft),
            park_lock=state.lock,
        )
        with state.lock:
            state.running = (control, row)
        try:
            runtime._run_turn(sid, row, lock, control)
        finally:
            with state.lock:
                state.running = None
            if own:
                lock.release()

    def _run_turn(self, sid, row, lock, control):
        runtime = self.runtime
        log = runtime.read_log(sid)
        log.lock = lock
        try:
            records, pending, old = turn_records(log)
            begun = any(
                attribution.is_turn_input(r.entry) and r.entry.get("inbox") == row["id"]
                for r in records
            )
        except timeline.NewerLog:
            raise
        except Exception:
            upgrade.record_failure(log, row["id"], traceback.format_exc())
            runtime.store.finish(row["arrival"], sid, "failed")
            return
        ask = timeline.parked_ask(pending, current_turn=True)
        payload = json.loads(row["payload"])
        if payload.get("reply_to") is not None:
            # An old inbox can contain submissions from before admission checks.
            expected = payload.get("reply_kind")
            entry = ask.entry if ask is not None else None
            kind = entry.get("kind", "permission") if entry is not None else None
            private = runtime.private_ask_kinds
            matches = (
                entry is not None and entry.id == payload["reply_to"]
                and (not expected or expected == kind)
                and (kind not in private or expected == kind)
                and (not attribution.requires_user(entry)
                     or attribution.is_user_source(row["source"]))
                and (payload.get("choice") is None or picked(ask, payload))
            )
            if matches:
                turn_id = pending[0].entry.get("inbox")
                runtime.take_reply(sid, row, ask, turn_id, log, control)
            else:
                # A stale submission in an older inbox cannot become a new turn.
                runtime.store.mark_steer_done(row["id"])
            return
        if pending and pending[0].entry.get("inbox") != row["id"]:
            raise ValueError("the queued message is not the log's open turn")
        if begun and not pending:
            raise ValueError("no open turn to resume")
        try:
            if old:
                runtime.upgrade_session(sid, log)
                records = log.branch()
        except timeline.NewerLog:
            raise
        except Exception:
            upgrade.record_failure(log, row["id"], traceback.format_exc())
            runtime.store.finish(row["arrival"], sid, "failed")
            return
        payload = json.loads(row["payload"])
        text = payload["text"]
        child_entries = _payload_entries(payload)
        workspace = Path(runtime.store.session(sid)["workspace"])
        if not workspace.is_dir():
            runtime.end_for_missing_workspace(
                sid, row, log, records, begun, text, workspace,
                child_entries=child_entries,
            )
            return
        try:
            session = runtime.harness_for_turn(sid, log, records, begun)
        except Exception:
            if begun:
                for fields in timeline.closing_results(
                    log.unfinished_turn(), replay.UNKNOWN, "unknown"
                ):
                    log.append("tool", **fields)
            else:
                user_fields = {
                    "inbox": row["id"], "source": row["source"], "text": text,
                }
                if child_entries:
                    user_fields["entries"] = child_entries
                log.append("user", **user_fields)
            runtime.store.begin(row["arrival"], sid)
            log.append(
                "turn_end", inbox=row["id"], outcome="crashed",
                error=traceback.format_exc(),
            )
            runtime.store.finish(row["arrival"], sid, "failed")
            runtime.turn_ended(sid, "crashed")
            return
        if begun:
            turn = session.resume(inbox=row["id"], control=control)
        else:
            carried = {
                name: payload[name] for name in runtime.message_fields()
                if payload.get(name) is not None and name != "attachments"
            }
            if child_entries:
                carried["entries"] = child_entries
            turn = session.open_turn(
                text, inbox=row["id"], source=row["source"], control=control,
                continued=bool(payload.get("continue")),
                queued=bool(payload.get("follow_up")),
                carried=carried,
            )
        runtime.store.begin(row["arrival"], sid)
        runtime.finish_turn(sid, session, turn, row)

    def end_for_missing_workspace(self, sid, row, log, records, begun, text, workspace,
                                  *, child_entries=()):
        """End the turn, its history kept: no command can run in a folder that
        isn't there."""
        runtime = self.runtime
        if begun:
            for fields in timeline.closing_results(
                log.unfinished_turn(), replay.UNKNOWN, "unknown"
            ):
                log.append("tool", **fields)
        else:
            user_fields = {
                "inbox": row["id"], "source": row["source"], "text": text,
            }
            if child_entries:
                user_fields["entries"] = child_entries
            log.append("user", **user_fields)
        runtime.store.begin(row["arrival"], sid)
        log.append(
            "turn_end", inbox=row["id"], outcome="failed",
            detail=f"this session's workspace {workspace} is gone",
        )
        runtime.store.finish(row["arrival"], sid, "failed")
        runtime.turn_ended(sid, "failed")

    def finish_turn(self, sid, session, turn, row):
        """Run the turn, then settle its row: done, or still delivered while it
        waits on an ask."""
        runtime = self.runtime
        try:
            outcome = session.run(turn)
        except Exception:
            end = next((r.entry for r in reversed(session.log.branch())
                        if entries.has(r.entry, "turn_end")
                        and r.entry.get("inbox") == row["id"]), None)
            if end is None:
                raise
            outcome = end.get("outcome")
        if outcome == "waiting":
            ask = timeline.parked_ask(session.log.unfinished_turn(), current_turn=True)
            runtime.store.park(sid, ask, row["id"])
            runtime.turn_ended(sid, outcome)
            return
        ended = "failed" if outcome in FAILED else "idle"
        with runtime.state(sid).lock:
            # Its stops done with it; its rows in step up to its turn_end
            runtime.store.finish(row["arrival"], sid, ended, session.log.last_seq)
        runtime.turn_ended(sid, outcome)

    def take_reply(self, sid, row, ask, turn_id, log, control):
        """The parked session's next message is the reply to its ask: what it
        answers is the reply step's (a service's, permissions/service.py: the box's
        choice, or words the consent check reads), or else the choice picked
        at the box alone. A yes runs the call; anything else cancels the ask
        and joins the turn as a message. The box's No declines the call and
        ends the turn."""
        runtime = self.runtime
        payload = json.loads(row["payload"])
        turn_row, = runtime.store.query(
            "SELECT * FROM inbox WHERE session = ? AND id = ?", sid, turn_id)
        if upgrade.file_format(log.path) < timeline.LOG_FORMAT:
            try:
                runtime.upgrade_session(sid, log)
            except timeline.NewerLog:
                raise
            except Exception:
                upgrade.record_failure(log, row["id"], traceback.format_exc())
                runtime.store.mark_steer_done(row["id"])
                return
            ask = timeline.parked_ask(log.unfinished_turn(), current_turn=True)
        entry = ask.entry
        session = runtime.harness(sid, log)
        text = payload["text"]
        choice, verdict = runtime.reply_step(sid, ask, payload)
        yes = choice is not None and choice != NO
        fields = {
            "ask": entry.id, "yes": yes, "inbox": row["id"],
            "source": row["source"], "text": text, "consent": verdict,
        }
        if child_entries := _payload_entries(payload):
            fields["entries"] = child_entries
        if payload.get("sender_session"):
            fields["sender_session"] = payload["sender_session"]
        if choice is not None:
            fields["choice"] = choice
        if kind := entry.get("kind"):
            fields["kind"] = kind
        if kind == "question":
            fields["yes"] = False
            if payload.get("choice") is not None:
                fields["text"] = entry.get("choice_labels").get(choice, text)
        turn = session.reply(fields, control=control)
        runtime.store.close_ask(entry.id, row["id"], fields["yes"], row["source"])
        state = runtime.state(sid)
        with state.lock:
            # A stop now belongs to the parked turn, not to the reply
            state.running = (control, turn_row)
        runtime.finish_turn(sid, session, turn, turn_row)

    def reply_step(self, sid, ask, payload):
        """(choice, verdict) for a reply to a parked ask."""
        runtime = self.runtime
        for service in runtime.services.values():
            answer = service.reply(sid, ask, payload)
            if answer is not None:
                return answer
        return picked(ask, payload), None

    def upgrade_session(self, sid, log):
        """Ask the current harness for its header, then restore it from the upgrade."""
        runtime = self.runtime
        info = runtime.store.session(sid)
        fresh = fresh_log(runtime.paths.sessions, info["model"])
        session = runtime.make_session(sid, info, log=fresh, ask=PARK)
        session.header_fields = {"row": rebuild.header_row(info)}
        upgrade.upgrade_file(log, session.session_record())
        session.restore(log)
        runtime.session_built(sid, session)
        runtime.state(sid).harness = session

    def harness_for_turn(self, sid, log, records, begun):
        """The harness a turn runs on. A new turn takes what the user set
        since the last one (mode, model, workspace, effort, the judge),
        logged as a switch, a note the model reads when it names what the
        agent knows of itself; a begun one (parked, or cut off by a restart)
        keeps what it started with."""
        runtime = self.runtime
        info = runtime.store.session(sid)
        switch = json.loads(info["switch"] or "{}")
        logged = log.running_with()
        if begun:
            # Built as it started, if it must be built again
            before = {name: logged[name] for name in switch if name in logged}
            return runtime.harness(sid, log, info={**info, **before})
        if not switch:
            return runtime.harness(sid, log)
        runtime.state(sid).harness = None
        # The effort and the judge have no column of their own: the switch
        # carries them
        session = runtime.harness(sid, log, info={**info, **switch})
        changes = {
            name: (logged.get(name), new) for name, new in switch.items()
            if logged.get(name) != new
        }
        if changes and records:
            moved = {
                "from": {name: old for name, (old, _) in changes.items()},
                "to": {name: new for name, (_, new) in changes.items()},
                "settings": session.fresh_settings,
            }
            seen = {k: v for k, v in changes.items() if k in timeline.MODEL_VISIBLE}
            if "model" in changes:
                # A new model loses the cache anyway: the system message is
                # written again for it, which names it
                session.rebuild_system("model")
            if seen:
                # The model reads what changed of what it knows of itself
                session.write("note", kind="switch",
                              text=switch_note(seen, session), **moved)
            else:
                # The judge alone: nothing the agent knows changed
                session.write("switch", **moved)
            session.settings = session.fresh_settings
        runtime.store.update_session(sid, switch=None)
        return session

    def harness(self, sid, log=None, info=None):
        """Build once per runtime; the harness owns its history on disk.
        `info`: the session's row, or what its log runs with (a begun turn)."""
        runtime = self.runtime
        state = runtime.state(sid)
        if state.harness is None:
            info = info or runtime.store.session(sid)
            session = runtime.make_session(sid, info, log=log or runtime.log(sid),
                                        ask=PARK)
            # Its header, when its first turn writes it, carries its row
            session.header_fields = {"row": rebuild.header_row(info)}
            state.harness = session
            runtime.session_built(sid, session)
        elif log is not None:
            session = state.harness
            session.log.lock = log.lock
            session.log.last_seq = None
            if session.log.branch():
                session.restore(session.log)
        return state.harness

    def append_between_turns(self, sid, refused, type, **fields):
        """Append a record to a session's log while no turn runs or waits for
        an answer (a grant taken back, permissions/service.py); else Refused,
        with `refused`."""
        runtime = self.runtime
        state = runtime.state(sid)
        with state.lock:
            row = runtime.store.session(sid)
            if (state.runner is not None or runtime.store.has_queued(sid)
                    or row["state"] in ("running", "waiting")):
                raise Refused(refused)
            lock = runtime.session_lock(sid)
            try:
                lock.acquire()
            except Busy as error:
                raise Refused(str(error)) from None
            try:
                runtime.log(sid, lock).append(type, **fields)
            finally:
                lock.release()

    def prepare_side_writer(self, sid, *, timeout=10.0):
        """Check for a writable log and finish any idle upgrade before billing."""
        self._side_record(sid, None, timeout)

    def append_side_record(self, sid, entry, *, timeout=10.0):
        """Append a non-turn entry through the session's single writer."""
        return self._side_record(sid, entry, timeout)

    def _side_record(self, sid, entry, timeout):
        runtime = self.runtime
        if runtime.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        state = runtime.state(sid)
        deadline = time.monotonic() + timeout
        while True:
            try:
                with state.lock:
                    if runtime.store.session(sid) is None:
                        raise Refused(f"no session {sid!r}")
                    if state.runner is None:
                        lock = runtime.session_lock(sid)
                        lock.acquire()
                        try:
                            log = self._prepare_idle_side_log(sid, state, lock)
                            return self._use_side_log(log, entry)
                        finally:
                            lock.release()
                    harness = state.harness
                    if harness is not None:
                        log = harness.log
                        if (
                            log.lock is not None and log.lock.held
                            and upgrade.file_format(log.path) == timeline.LOG_FORMAT
                        ):
                            return self._use_side_log(log, entry)
            except (Busy, timeline.NewerLog) as error:
                raise Refused(str(error)) from None
            if time.monotonic() >= deadline:
                raise Refused("wait limit exceeded for session writer")
            time.sleep(0.02)

    @staticmethod
    def _use_side_log(log, entry):
        if entry is not None:
            return log.append(entry)
        try:
            with log.path.open("ab"):
                return None
        except OSError:
            raise Refused("The session log could not be opened for append.") from None

    def _prepare_idle_side_log(self, sid, state, lock):
        runtime = self.runtime
        log = runtime.log(sid, lock)
        found = upgrade.file_format(log.path)
        if found < timeline.LOG_FORMAT:
            if found == 1 and not log.path.exists():
                raise Refused("this session has no log to write")
            try:
                if found == 1:
                    runtime.upgrade_session(sid, log)
                else:
                    upgrade.upgrade_file(log, None)
            except timeline.NewerLog:
                raise
            except Exception as error:
                raise Refused(
                    f"Could not upgrade this session's log: {error}"
                ) from None
            finally:
                state.harness = None
        return log

    def run_on_session(self, sid, name, fn, *, if_busy=None, precheck=None):
        """Run `fn(session)` on a background thread held as `sid`'s runner.

        When the session is busy, call `if_busy(harness, row)` under
        `state.lock` and return its result (or raise Refused when `if_busy`
        is None). When idle, run `precheck()` under `state.lock` before
        holding the runner and starting the thread, then return None.
        """
        runtime = self.runtime
        if runtime.store.session(sid) is None:
            raise Refused(f"no session {sid!r}")
        state = runtime.state(sid)
        with state.lock:
            if not runtime.accepting:
                raise Refused("the server is shutting down")
            row = runtime.store.session(sid)
            if row is None:
                raise Refused(f"no session {sid!r}")
            busy = (state.runner is not None or runtime.store.has_queued(sid)
                    or row["state"] in ("running", "waiting"))
            if busy:
                if if_busy is None:
                    raise Refused("the session is busy")
                return if_busy(state.harness, row)
            if precheck is not None:
                precheck()
            thread = threading.Thread(
                target=runtime._run_on_session, args=(sid, state, fn),
                name=f"{name}-{sid}", daemon=True,
            )
            runtime.hold_runner(state, thread)
        thread.start()
        return None

    def _run_on_session(self, sid, state, fn):
        runtime = self.runtime
        lock = runtime.session_lock(sid)
        try:
            lock.acquire()
            try:
                fn(runtime.harness(sid, runtime.log(sid, lock)))
            finally:
                with state.lock:
                    lock.release()
        except Exception:
            traceback.print_exc()
        finally:
            with state.lock:
                # Its last word is still to say, as a turn runner's is
                runtime.let_go(state, leaving=True)
        try:
            settled = runtime.settled(sid)
            if settled is not None:
                runtime.publish(sid, {"settled": settled})
            runtime.dispatch(sid)
        finally:
            runtime.said_last_word(state)
