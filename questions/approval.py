"""Exact-call proposals and single-use approval from the user's own answer."""

import json

from core import history, tools
from core.hooks import Deny


def proposal_for(name, arguments, context):
    entry = tools.entry_of(name)
    prepare = entry and entry.get("user_approval")
    if prepare is None:
        raise ValueError("This tool does not accept exact-change proposals.")
    if entry.get("ready") and not entry["ready"](context):
        raise ValueError("This tool is unavailable in this session.")
    args, error = tools.check(name, json.dumps(arguments))
    if error:
        raise ValueError(error)
    proposal = prepare(args, context)
    return {"tool": name, "state": proposal["state"]}, proposal["text"]


def approved_ask(session, proposal):
    records = session.log.branch()
    # A rewind cannot reuse an approval whose change already ran.
    used = {r.get("notes", {}).get("user_approval", {}).get("ask")
            for r in session.log.records() if r["type"] == "tool"}
    asks = {
        r["ask"] for r in records if r["type"] == "ask"
        and r.get("notes", {}).get("approval_proposal") == proposal
        and r.get("notes", {}).get("approval_session") == str(session.log.path)
    }
    for record in reversed(records):
        if record["type"] != "answer" or record.get("ask") not in asks:
            continue
        # MCP replies come from another agent, not the human using an app.
        if (record.get("choice") == "yes" and record.get("source")
                and record["source"] != "mcp"
                and not history.is_notice(record) and record["ask"] not in used):
            return record["ask"]
        # A newer refusal supersedes any earlier permission for this proposal.
        return None
    return None


def check_approval(call):
    entry = tools.entry_of(call.name)
    if not entry or entry.get("user_approval") is None:
        return None
    session = call.turn.session
    if session.settings.get("mode") == "readOnly":
        return Deny("Not changed: this session is readOnly.")
    try:
        proposal, _ = proposal_for(call.name, call.args, session.tool_context)
    except (ValueError, OSError) as error:
        return Deny(f"Not changed: {error}")
    ask = approved_ask(session, proposal)
    if ask is None:
        return Deny(
            f"Not changed: {tools.question_tool()} needs the user's approval "
            "of this exact proposed_call and the file's current contents."
        )
    call.notes["user_approval"] = {"ask": ask, "state": proposal["state"]}
    return None
