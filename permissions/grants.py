"""The permissions memory: calls that run without an ask because someone
already said yes to them.

Three kinds of grant, each read from where it is kept, never held in memory
alone, so a restart, a switch of mode or a second client sees the same:
  plan     the calls of an approved plan (tool.py), each exactly as
           listed, until the user's next message: from the log
  session  "Yes, and don't ask again this session for …", picked by the user
           (an `answer` record's choice) or by the judge (its verdict on the
           call): from the log; a `forget` record takes one back
  saved    the same, saved by the user: from ~/.zero/permissions.toml
           (paths.permissions_file), for every session
A call a grant covers is green: its ask (asks.py, or auto+'s
Permissions) isn't put to anyone, and the repeat guard lets it through; the
caps' asks still hold. The judge's grants count only where the judge is the
approver. The model is told why a call ran unasked, in a remark.

What "don't ask again" offers is each tool's own (its Grant, core/tools.py;
from Codex, OpenCode and Claude Code): for bash, each part's prefix
(shell/commands.py: `git push`, `rm`, `docker compose up`); for a local fetch,
its server; for a file tool, its path. A later call is covered when every item
it would be granted already is. A grant is {"tool", <its field>: [items]}, as
the user's saved file keeps it.
"""

import json
import re
import secrets
import tomllib
from datetime import datetime
from pathlib import Path

from core import history, tools
from core.asking import SAVED, waiting_ask
from core.hooks import Hook

PLAN = "propose_plan"
SAVED_HEAD = (
    "# Permissions saved from an ask in ZERO: calls these cover run in "
    "every\n# session without asking. /permissions lists them; delete a block "
    "to take one back.\n"
)

PLAN_REMARK = "(Run without review: one of the calls of the plan {who} approved.)"
SESSION_REMARK = "(Run without review: {who} allowed {what} for this session.)"
SAVED_REMARK = "(Run without review: the user saved a permission for {what}.)"


def offer(call):
    """The grant "don't ask again" would make for this call (its tool's
    Grant), or None."""
    declared = tools.declared_grant(call.name)
    if declared is None:
        return None
    found = declared.of(call.args or {})
    return {"tool": call.name, declared.field: found} if found else None


def covered(grants, call):
    """Whether these grants (all for the call's tool) cover the call."""
    want = offer(call)
    if want is None:
        return False
    field = tools.declared_grant(call.name).field
    have = {x for g in grants for x in g.get(field, ())}
    return set(want[field]) <= have


def describe(grant):
    """A grant in a few words, its tool's ("`cd` and `git push` commands")."""
    declared = tools.declared_grant(grant["tool"])
    if declared is not None:
        return declared.words(grant)
    # A saved grant for a tool this ZERO doesn't have
    return f"{grant['tool']} to " + ", ".join(sorted(items(grant)))


def items(grant):
    """What a grant names: its prefixes, servers or paths."""
    return {x for field in tools.grant_fields() for x in grant.get(field, ())}


def call_key(name, arguments):
    """A call as the loop keys it (core/records.py's Call.key)."""
    return (name, tools.normalise(arguments))


def plan_grants(records):
    """The calls of plans approved since the user's last message: key → who
    approved ("the user" or "the judge")."""
    approved = {}
    for record in records:
        if record["type"] == "user" and not history.is_notice(record):
            approved = {}
        elif (record["type"] == "tool" and record.get("tool") == PLAN
              and record.get("ran") and not record.get("denied")):
            who = "the judge" if record.get("notes", {}).get("judge") else (
                "the user"
            )
            try:
                calls = json.loads(record["arguments"])["calls"]
            except (ValueError, KeyError, TypeError):
                continue
            for call in calls:
                arguments = json.dumps(call["arguments"])
                approved[call_key(call["tool"], arguments)] = who
    return approved


def session_grants(records):
    """This session's grants, oldest first: {id, by, scope, grant, time}."""
    offers, grants = {}, {}
    for record in records:
        kind = record["type"]
        if kind == "ask" and record.get("notes", {}).get("offer"):
            offers[record["ask"]] = record["notes"]["offer"]
        elif (kind == "answer" and record.get("yes")
              and record.get("choice") in ("session", "saved")
              and record["ask"] in offers):
            grants[record["ask"]] = {
                "id": record["ask"], "by": "user", "scope": record["choice"],
                "grant": offers[record["ask"]], "time": record.get("time"),
            }
        elif kind == "tool" and record.get("ran"):
            notes = record.get("notes", {})
            verdict = notes.get("judge") or {}
            if verdict.get("decision") == "session" and notes.get("offer"):
                grants[record["call"]] = {
                    "id": record["call"], "by": "judge", "scope": "session",
                    "grant": notes["offer"], "time": record.get("time"),
                }
        elif kind == "forget":
            grants.pop(record.get("grant"), None)
    return list(grants.values())


def read_saved(path):
    """The user's saved grants: {id, by, scope, grant, time}. A file that
    can't be read counts as none (fewer grants only means more asks)."""
    if path is None:
        return []
    try:
        data = tomllib.loads(Path(path).read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    saved = []
    for entry in data.get("allow", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("tool"), str):
            continue
        grant = {"tool": entry["tool"]}
        for field in tools.grant_fields():
            if isinstance(entry.get(field), list):
                grant[field] = [str(x) for x in entry[field]]
        saved.append({
            "id": str(entry.get("id", "")), "by": "user", "scope": "saved",
            "grant": grant, "time": entry.get("saved"),
        })
    return saved


def save(path, grant, session=None):
    """Add a grant to the user's file; returns its id."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    grant_id = f"p-{secrets.token_hex(3)}"
    lines = [
        "", "[[allow]]", f"id = {json.dumps(grant_id)}",
        f"tool = {json.dumps(grant['tool'])}",
    ]
    for field in tools.grant_fields():
        if field in grant:
            items = ", ".join(json.dumps(x, ensure_ascii=False) for x in grant[field])
            lines.append(f"{field} = [{items}]")
    lines.append(f"saved = {json.dumps(datetime.now().astimezone().isoformat())}")
    if session is not None:
        lines.append(f"session = {json.dumps(session)}")
    text = path.read_text() if path.exists() else SAVED_HEAD
    path.write_text(text.rstrip("\n") + "\n" + "\n".join(lines) + "\n")
    return grant_id


def save_grant(path, ask, choice, session):
    """A "saved" answer to an ask: its grant to the user's file at `path`
    (None: no file, as in the evals, where it counts for the session)."""
    if choice == SAVED and path is not None:
        save(path, ask["notes"]["offer"], session=session)


def forget_saved(path, grant_id):
    """Remove a saved grant's block; whether it was there."""
    path = Path(path)
    if not path.exists():
        return False
    blocks = re.split(r"(?m)^(?=\[\[allow\]\])", path.read_text())
    marker = f"id = {json.dumps(grant_id)}"
    kept = [b for b in blocks if marker not in b.splitlines()]
    if len(kept) == len(blocks):
        return False
    path.write_text("".join(kept))
    return True


def listing(records, saved_path, judged):
    """Every grant that holds in a session: its plan's calls aside. A "saved"
    answer counts from the user's file; without one (the evals), for the
    session."""
    grants = []
    for grant in session_grants(records):
        if grant["by"] == "judge" and not judged:
            continue
        if grant["scope"] == "saved":
            if saved_path is not None:
                continue
            grant = {**grant, "scope": "session"}
        grants.append(grant)
    return grants + read_saved(saved_path)


class GrantCheck(Hook):
    """Marks what a call's "don't ask again" would grant (`offer`), and a call
    a grant covers (`granted`), which core/asking.py's ask_of() then passes
    over. `saved`: the user's file, None for none (evals, tests); `judged`:
    whether the judge is this session's approver, so its grants count."""

    def __init__(self, saved=None, judged=False):
        self.saved = saved
        self.judged = judged

    def before_tool(self, call):
        if call.error or call.name == PLAN:
            return None
        found = offer(call)
        if found is not None:
            call.notes["offer"] = found
            # The words the box and the consent check put to the user
            call.notes["offer_text"] = describe(found)
        granted = self._granted(call)
        if granted is not None:
            call.notes["granted"] = granted
        return None

    def approve(self, call):
        """A green call's ask was passed over: the model is told why."""
        granted = call.notes.get("granted")
        if granted is None or waiting_ask(call.notes, granted=False) is None:
            return None
        if granted["scope"] == "plan":
            text = PLAN_REMARK.format(who=granted["by"])
        elif granted["scope"] == "saved":
            text = SAVED_REMARK.format(what=describe(granted["grant"]))
        else:
            who = "the judge" if granted["by"] == "judge" else "the user"
            text = SESSION_REMARK.format(who=who, what=describe(granted["grant"]))
        call.remarks.append(text)
        return None

    def _granted(self, call):
        records = call.turn.session.log.records()
        who = plan_grants(records).get(call.key)
        if who is not None:
            return {"scope": "plan", "by": who}
        entries = [e for e in listing(records, self.saved, self.judged)
                   if e["grant"]["tool"] == call.name]
        if not entries or not covered([e["grant"] for e in entries], call):
            return None
        # The grants that cover some part of it: one, or several together
        want = offer(call)
        used = [e for e in entries if items(e["grant"]) & items(want)]
        scope = "saved" if all(e["scope"] == "saved" for e in used) else "session"
        by = "judge" if any(e["by"] == "judge" for e in used) else "user"
        return {"scope": scope, "by": by, "grant": want,
                "ids": [e["id"] for e in used]}
