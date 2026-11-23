"""Exact-call proposals and single-use approval from the user's own answer."""

import json

from core import attribution, entries, tools
from core.hooks import Deny


def question_tool():
    """The declared question tool's name, if registered."""
    found = tools.tools_with_trait("asks_user")
    return found[0][0] if found else None


def proposal_for(name, arguments, context):
    entry = tools.entry_of(name)
    meta = tools.metadata_of(name, "questions") or {}
    prepare = meta.get("user_approval")
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
    proposal = entries.freeze(proposal)
    # A rewind cannot reuse an approval whose change already ran.
    used = {(r.entry.get("notes") or {}).get("user_approval", {}).get("ask")
            for r in session.log.records() if entries.has(r.entry, "tool_result")}
    asks = {
        r.entry.id for r in records if entries.has(r.entry, "ask")
        and (notes := r.entry.get("notes") or {}).get("approval_proposal") == proposal
        and notes.get("approval_session") == str(session.log.path)
    }
    for record in reversed(records):
        answer = record.entry
        if not entries.has(answer, "answer"):
            continue
        ask = entries.linked_to(answer, "answers")
        if ask not in asks:
            continue
        event = attribution.interaction(record)
        if (answer.get("choice") == "yes"
                and attribution.is_user_source(event["sender"])
                and event["target"].get("ask") == ask and ask not in used):
            return ask
        # A newer refusal supersedes any earlier permission for this proposal.
        return None
    return None


def check_approval(call):
    meta = tools.metadata_of(call.name, "questions") or {}
    if meta.get("user_approval") is None:
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
            f"Not changed: {question_tool()} needs the user's approval "
            "of this exact proposed_call and the file's current contents."
        )
    call.notes["user_approval"] = {"ask": ask, "state": proposal["state"]}
    return None
