"""Plain text from log records and ask events."""

import json
from datetime import datetime


def duration(record, started=None, restarts=0):
    if started and restarts:
        seconds = max(0, int((
            datetime.fromisoformat(record["time"]) - datetime.fromisoformat(started)
        ).total_seconds()))
        count = "once" if restarts == 1 else f"{restarts} times"
        return f"{seconds} s in all, resumed {count}"
    return f"{record.get('seconds', 0)} s"


def short_tokens(count):
    """A count of tokens as the meter shows it: 950, 42k, 1M, 1.5M."""
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.0f}k"
    return f"{count / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"


def meter_text(context):
    """The context's meter in a few words, `context 42k/1M · cache 96%`; ""
    with nothing to show."""
    if not context or not context.get("tokens"):
        return ""
    text = f"context {short_tokens(context['tokens'])}"
    if context.get("line"):
        text += f"/{short_tokens(context['line'])}"
    if context.get("cached_share") is not None:
        text += f" · cache {context['cached_share']:.0%}"
    return text


# Who or what started a compaction, as a transcript says it
COMPACTED_BY = {"line": "at its line", "zero": "asked by ZERO",
                "user": "asked by you", "refused": "after the server refused "
                "a request as too long"}


def compaction_text(record):
    """A compaction, or a failed one, in a line: why, and the tokens before
    and after."""
    why = COMPACTED_BY.get(record.get("reason"), record.get("reason"))
    if record["type"] == "compaction_failed":
        return f"compaction {why} failed: {record.get('error')}"
    before = short_tokens(record.get("tokens_before") or 0)
    after = short_tokens(record.get("tokens_after") or 0)
    how = "handed off" if record.get("summary") else "old outputs cleared"
    return f"context compacted {why}, {how}: {before} → {after} tokens"


def rewind_text(record):
    """A rewind, in a line: the records it left behind."""
    return (f"rewound here: records {record['left_from']}–{record['left_to']} "
            "left behind, kept in the log; files stay as they are")


# What a client says to `session/compact`'s reply
COMPACTING = {"now": "Compacting now.",
              "before_next_step": "Compacting before the turn's next step."}


def clipped(text):
    lines = text.splitlines()
    if len(lines) <= 10:
        return lines
    return lines[:5] + [f"[... {len(lines) - 10} lines cut ...]"] + lines[-5:]


def outcome_of(result):
    """A tool record's result as (ok, exit code or None, wall time, output
    lines), for the CLI's command blocks. bash's results lead with `Exit
    code: N` (or how it was stopped) and `Wall time: X s`, then `Output:`."""
    head, marker, output = result.partition("\nOutput:\n")
    if not marker:
        refused = result.startswith("Not run")
        return not refused, None, "", result.splitlines()
    status = head.splitlines()
    code = None
    if status and status[0].startswith("Exit code: "):
        word = status[0][len("Exit code: "):].split()[0]
        code = int(word) if word.lstrip("-").isdigit() else None
    seconds = next((line[len("Wall time: "):] for line in status
                    if line.startswith("Wall time: ")), "")
    lines = [] if output.strip() == "(no output)" else output.splitlines()
    return code == 0, code, seconds, lines


def who(source, mine=None):
    """Whose message: yours (from this terminal or another, with no mark for
    that), or a job's."""
    if source in (None, mine, "cli") or source.startswith("terminal-"):
        return "you"
    return source


def divider(title, workspace, width=80):
    text = f"── {title} · {workspace} "
    return text + "─" * max(3, width - len(text))


def jobs_text(jobs, sessions=False):
    """The server's job rows, each with its state in words (`state_text`)."""
    return "\n".join(
        f"  {row['id']}  {row['session'] + '  ' if sessions else ''}"
        f"{row['state_text']}  {row['command']}"
        for row in jobs
    )


# What the rules put before an ask's reason; the agent's own words follow
# The judge's prefix only in an older log, from when the judge could ask
REASON_PREFIXES = ("rule: ", "it asks to run outside the sandbox: ",
                   "it fetches from a local address: ",
                   "the judge wants the user to decide: ")


def reason(why):
    """The agent's own reason in an ask's `why`, without what the rules
    say before it."""
    why = why or ""
    for prefix in REASON_PREFIXES:
        if why.startswith(prefix):
            why = why[len(prefix):]
    return why.strip()


def ask_line(ask):
    """An ask on one line, for plain output: its command and its reason."""
    why = reason(ask.get("why"))
    return f"? {ask['tool']}: {ask['command']}" + (f" ({why})" if why else "")


def main_argument(args, raw):
    """bash's command, a file tool's path, a fetch's URL, a search's query
   , or else the raw arguments."""
    for name in ("command", "path", "url", "query"):
        if name in args:
            return args[name]
    return raw


def plan_text(args):
    """A plan as its text, then its calls numbered, one line each."""
    lines = [str(args.get("plan", "")).strip(), "", "calls:"]
    for n, call in enumerate(args.get("calls") or [], 1):
        if isinstance(call, dict) and isinstance(call.get("arguments"), dict):
            what = main_argument(call["arguments"], json.dumps(call["arguments"]))
            lines.append(f"{n}. {call.get('tool')}: {what}")
        else:
            lines.append(f"{n}. {call}")
    return "\n".join(lines)


def command_of(call):
    """A call as a line: its main argument (main_argument); a plan as its
    text and calls (plan_text), whose first line is the plan's."""
    arguments = call["function"]["arguments"]
    try:
        args = json.loads(arguments)
        if call["function"].get("name") == "propose_plan" and "plan" in args:
            return plan_text(args)
        return main_argument(args, arguments)
    except (ValueError, AttributeError, TypeError):
        return arguments


def grants_text(grants):
    """/permissions' list: numbered, who granted it and for how long."""
    if not grants:
        return "Nothing runs without asking here, beyond an approved plan's calls."
    lines = []
    for n, grant in enumerate(grants, 1):
        who = "the judge" if grant["by"] == "judge" else "you"
        how = "in every session" if grant["scope"] == "saved" else (
            "in this session")
        lines.append(f"{n}. {grant['description']}: allowed by {who}, {how}")
    lines.append("\n/permissions forget N takes one back.")
    return "\n".join(lines)


# The judge's settings, as a switch line names them
SWITCH_NAMES = {"judge_model": "judge", "judge_thinking_effort": "judge's thinking"}


def switch_text(record):
    """A switch as the transcript shows it: "mode: default → auto"; the
    judge's too, which the model doesn't read."""
    before = record.get("from", {})

    def said(name, value):
        # The judge's model, unset, is the session's own
        if name == "judge_model" and not value:
            return "the session's model"
        return value

    return " · ".join(
        f"{SWITCH_NAMES.get(name, name)}: {said(name, before.get(name, '?'))} "
        f"→ {said(name, value)}"
        for name, value in record.get("to", {}).items()
    )


class Renderer:
    def __init__(self, source=None):
        self.source = source
        self.started = None
        self.restarts = 0

    def event(self, event):
        record = event.get("record", {})
        kind = record.get("type")
        if kind == "user":
            if not record.get("steer"):
                self.started, self.restarts = record.get("time"), 0
            if record.get("continue"):
                return ["(you asked ZERO to go on)"]
            source = record.get("source")
            # This terminal echoed its own steers and replies as they were sent
            if self.source is not None and source == self.source and record.get(
                "steer"
            ):
                return []
            mark = " (steer)" if record.get("steer") else ""
            return [f"{who(source, self.source)}{mark}> {record['text']}"]
        if kind == "assistant":
            message = record["message"]
            lines = [message["content"]] if message.get("content") else []
            for call in message.get("tool_calls") or []:
                name = call["function"]["name"]
                name = "plan" if name == "propose_plan" else name
                lines.append(f"> {name}: {command_of(call)}")
            return lines
        if kind == "tool":
            return clipped(record["result"])
        if kind == "ask":
            # The CLI's box holds the question; the transcript keeps one line
            return [ask_line(record)]
        if kind == "answer":
            by = who(record.get("source"), self.source)
            if record["yes"]:
                return [f"({by}: approved)"]
            return [f"({by} replied; not run)"]
        if kind == "note":
            self.restarts += record.get("kind") == "restarted"
            return [record["text"]]
        if kind == "switch":
            return [f"({switch_text(record)})"]
        if kind in ("compaction", "compaction_failed"):
            return [f"({compaction_text(record)})"]
        if kind == "rewind":
            return [f"({rewind_text(record)})"]
        if kind == "turn_end":
            elapsed = duration(record, self.started, self.restarts)
            if record["outcome"] == "answered":
                return [f"({elapsed})"]
            why = record.get("detail") or ""
            if record.get("error"):
                why = record["error"].strip().splitlines()[-1]
            detail = f": {why}" if why else ""
            return [f"({record['outcome']}{detail}, {elapsed})"]
        return []
