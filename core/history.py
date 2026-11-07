"""Pure views of the session log: model messages and the evals' turn shape."""

from datetime import datetime

from core import tools

LOG_FORMAT = 2
UNKNOWN = (
    "Tool call interrupted before a result was recorded; its outcome is unknown."
)
# Before a message the user sent while a turn ran, in the model's copy
MID_TURN = "[The user wrote this while you were working.]"


class OldLog(ValueError):
    """The server must upgrade this log before handing it to the harness."""


# What the user may change between a session's turns, each logged as a
# switch: its `to` values and the session's settings from then on. A change
# to what the agent knows of itself (MODEL_VISIBLE) is a `switch` note the
# model reads; the judge's alone is a `switch` record it doesn't
MODEL_VISIBLE = ("mode", "model", "workspace", "thinking_effort")
SWITCHABLE = (*MODEL_VISIBLE, "judge_model", "judge_thinking_effort")
# Two settings a log written before 0.6.2.8 names otherwise
OLD_NAMES = {"effort": "thinking_effort", "judge_effort": "judge_thinking_effort"}
# A step's and a turn's field a log written before 0.6.2.8 names otherwise
OLD_LOG_FIELDS = {"plugin_errors": "hook_errors"}


def renamed(values):
    """A log's settings or switch under today's names."""
    return {OLD_NAMES.get(name, name): value for name, value in values.items()}


def is_switch(record):
    if record["type"] == "switch":
        return True
    return record["type"] == "note" and record.get("kind") == "switch"


def chosen_judge(settings):
    """The judge a session's settings hold, as the user left it: its model
    (None: the session's own) and its effort. A log written before the judge
    was the session's own setting has only the judge it ran with, kept as it
    was; None when it says nothing (no judge in its mode)."""
    if "judge_model" in settings:
        return {"judge_model": settings["judge_model"],
                "judge_thinking_effort": settings.get("judge_thinking_effort")}
    ran = settings.get("judge")
    if ran:
        return {"judge_model": ran.get("model"),
                "judge_thinking_effort": ran.get("effort")}
    return None


def running_with(records):
    """The mode, model, workspace, effort and judge the log runs with: its
    header's, then each switch's; {} before the first turn. The judge's two
    come from the settings it runs with now, and are missing where those say
    nothing of them."""
    if not records or records[0].get("type") != "session":
        return {}
    head = records[0]
    now = {
        "mode": head.get("mode"),
        "model": head.get("model"),
        "workspace": head.get("settings", {}).get("workspace"),
        "thinking_effort": renamed(head.get("settings", {})).get(
            "thinking_effort"
        ),
    }
    for record in records:
        if is_switch(record):
            now.update(renamed(record["to"]))
    now.update(chosen_judge(settings_now(records)) or {})
    return now


def settings_now(records):
    """The settings the session runs with: its header's, or the last switch's."""
    settings = records[0]["settings"]
    for record in records:
        if is_switch(record):
            settings = record["settings"]
    return renamed(settings)


def log_format(records):
    if records and records[0].get("type") == "session":
        if records[0].get("format") == LOG_FORMAT:
            return LOG_FORMAT
    return 1


def message(record):
    """The message the model sees for one record, or None."""
    kind = record["type"]
    if kind == "session":
        return {"role": "system", "content": record["system_prompt"]}
    if kind == "user" and record.get("mid_turn"):
        return {"role": "user", "content": f"{MID_TURN}\n{record['text']}"}
    if kind in ("user", "note"):
        return {"role": "user", "content": record["text"]}
    if kind == "assistant" and not record.get("empty"):
        return record["message"]
    if kind == "tool":
        return {
            "role": "tool",
            "tool_call_id": record["call"],
            "content": record["result"],
        }
    return None


# The sources whose messages are notices from ZERO, not the user's words (a
# job's end): each part that sends them declares its own (jobs/tool.py)
NOTICES = set()


def is_notice(message):
    """Whether a message, a user record or a steer, is a notice."""
    return message.get("source") in NOTICES


def steer_fields(steer):
    """A message a running turn takes in, as its record's fields. The user's
    own are `mid_turn`, so the model reads that they came while it
    worked; a notice says what it is itself."""
    fields = {"text": steer["text"], "steer": True}
    for name in ("inbox", "source"):
        if steer.get(name) is not None:
            fields[name] = steer[name]
    if not is_notice(steer):
        fields["mid_turn"] = True
    return fields


def replay(records):
    return [m for record in records if (m := message(record)) is not None]


def turns(records):
    """Turns in the trajectory's shape; a restart keeps the same turn."""
    result = []
    steps = {}
    for record in records:
        kind = record["type"]
        if kind == "user":
            if record.get("steer") and result:
                result[-1].setdefault("steers", []).append(record["text"])
                continue
            result.append({"question": record["text"], "steps": []})
            steps = {}
        elif not result:
            continue
        elif kind == "assistant":
            step = {
                "step": record["step"],
                "content": record["message"].get("content"),
                "tool_calls": [],
                **{
                    name: record[name]
                    for name in (
                        "finish_reason", "prompt_tokens", "cached_tokens",
                        "completion_tokens", "retries", "model_seconds",
                    )
                },
                "seconds": 0.0,
            }
            if record.get("empty"):
                step["empty"] = True
            steps[record["step"]] = step
            result[-1]["steps"].append(step)
        elif kind == "tool" and record.get("closed") != "not_started":
            steps[record["step"]]["tool_calls"].append({
                **{name: record[name] for name in (
                    "tool", "arguments", "result", "bad_call", "denied"
                )},
                **record.get("notes", {}),
            })
        elif kind == "step" and record["step"] in steps:
            step = steps[record["step"]]
            step["seconds"] = record["seconds"]
            # A log written before 0.6.2.8 says plugin_errors
            errors = record.get("hook_errors") or record.get("plugin_errors")
            if errors:
                step["hook_errors"] = errors
        elif kind == "turn_end":
            result[-1].update({
                OLD_LOG_FIELDS.get(name, name): record[name]
                for name in (
                    "outcome", "seconds", "agent_seconds", "wait_seconds",
                    "detail", "answer", "error", "hook_errors", "plugin_errors",
                )
                if name in record
            })
    return result


def unfinished_turn(records):
    """The unfinished turn's records, or an empty list."""
    start = None
    for i, record in enumerate(records):
        if record["type"] == "user" and not record.get("steer"):
            start = i
        elif record["type"] == "turn_end":
            start = None
    return records[start:] if start is not None else []


def parked_ask(records):
    """The open turn's `ask` record still waiting for its reply, or None."""
    ask = None
    for record in unfinished_turn(records):
        if record["type"] == "ask":
            ask = record
        elif record["type"] == "answer" and ask and record["ask"] == ask["ask"]:
            ask = None
    return ask


def lost_reply(records):
    """A reply that cancelled an ask but was cut off before it joined the turn as
    a message: its `answer` record, or None. The restart repair writes it in."""
    answer = None
    for record in unfinished_turn(records):
        if record["type"] == "answer":
            answer = None if record["yes"] else record
        elif answer and record["type"] == "user" and (
            record.get("inbox") == answer["inbox"]
        ):
            answer = None
    return answer


def dangling_calls(records):
    """Calls without a result, paired with their assistant record's step."""
    calls = {}
    for record in records:
        if record["type"] == "assistant":
            for call in record["message"].get("tool_calls") or []:
                calls[call["id"]] = (record["step"], call)
        elif record["type"] == "tool":
            calls.pop(record["call"], None)
    return list(calls.values())


def closing_results(records, result, closed):
    """Result records for unresolved calls, without inventing an execution."""
    for step, raw in dangling_calls(records):
        name = raw["function"]["name"]
        arguments = raw["function"]["arguments"]
        _, error = tools.check(name, arguments)
        yield {
            "step": step, "call": raw["id"], "tool": name, "arguments": arguments,
            "result": result, "bad_call": bool(error), "denied": False, "ran": False,
            "notes": {}, "closed": closed,
        }


def restart_note(records, now=None, extra=""):
    """Describe the gap before repair writes anything to the log: how long
    ago the turn was cut off, that nothing came from the user since, and
    `extra`, what the hooks add (the files this turn read or wrote that
    changed since): the facts the model judges from whether going on still
    makes sense; no age limit in code."""
    last = datetime.fromisoformat(records[-1]["time"])
    now = now or datetime.now().astimezone()
    seconds = max(0, int((now - last).total_seconds()))
    text = (
        f"This turn was cut off {span(seconds)} ago, when ZERO stopped; "
        "it has just restarted. Nothing has come from the user since."
    )
    return f"[{text}{extra}]"


def span(seconds):
    """45 seconds, 2 minutes, 14 hours, 3 days: a gap, for the model."""
    for size, unit, below in (
        (1, "second", 60), (60, "minute", 120 * 60), (3600, "hour", 48 * 3600),
    ):
        if seconds < below:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'}"
    count = seconds // 86400
    return f"{count} day{'' if count == 1 else 's'}"


def final_answer(records):
    last = None
    for record in reversed(records):
        if record["type"] == "user":
            return None
        if record["type"] == "assistant":
            last = record
            break
    if last and not last.get("empty") and last["finish_reason"] != "length":
        message = last["message"]
        if not message.get("tool_calls") and (message.get("content") or "").strip():
            return message["content"]
    return None
