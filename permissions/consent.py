"""The consent check: is the user's reply to an ask a plain yes?

The third model part, beside the doer and the judge. It sees only the question
as the user saw it (the command, and why it was asked) and the user's reply,
never the session's history or any command output, so nothing a command printed
can talk it into a yes. It decides one thing: is the reply an unconditional yes
to running exactly this command, now? Anything else (a no, a question, a
condition, a change, hesitation) is not, and goes to the agent as a message. No
words are matched in code: "y" goes through it too. An error or a timeout is not
a yes. Who calls it: the reply step of a server that parks turns (service.py).
"""

import json
import time

from core.asking import NO, YES
from core.llm import complete

PROMPT = """
An assistant asked the user for permission before running a command on the
user's computer. You read the user's reply and decide one thing: is it a plain
yes to running exactly this command, now?

- Yes: the reply approves it without conditions, in any words ("y", "yes",
  "sure", "go ahead", "ok, do it", "why not").
- Not yes: a no; a question; a condition or a change ("yes, but with -n first",
  "only the first one"); hesitation ("hmm", "wait"); anything about something
  else. These go to the assistant as a message, and it can ask again.
- When unsure, it is not a yes.

Some questions offer more than a yes, under "choices": "session" (yes, and
the kind of command it names runs without asking for the rest of this
session) and "saved" (the same, in every session from now on). A reply that
asks not to be asked again for these picks "session" ("yes, and don't ask
again"); one that asks for that beyond this session picks "saved" ("always
allow these, from now on"). Any other yes is choice "yes". A reply asking for
more than a choice names (every command, anything at all) has a condition.

Answer with JSON only:
{"yes": true or false, "choice": "yes", "session" or "saved", "reason": "..."}
reason: a few words on how you read the reply.
""".strip()

SCHEMA = {
    "name": "consent",
    "schema": {
        "type": "object",
        "properties": {
            "yes": {"type": "boolean"},
            "choice": {"type": "string", "enum": ["yes", "session", "saved"]},
            "reason": {"type": "string"},
        },
        "required": ["yes", "choice", "reason"],
        "additionalProperties": False,
    },
}


class ConsentError(ValueError):
    """The reply isn't a verdict."""


def parse(content):
    """{yes, reason} from the model's reply, or ConsentError."""
    try:
        data = json.loads(content or "")
    except json.JSONDecodeError as e:
        raise ConsentError(f"not JSON: {content!r:.200}") from e
    if not isinstance(data, dict) or not isinstance(data.get("yes"), bool):
        raise ConsentError(f"no yes or no in {content!r:.200}")
    reason = data.get("reason")
    if not isinstance(reason, str):
        raise ConsentError("no reason given")
    verdict = {"yes": data["yes"], "reason": reason.strip()}
    # Lenient: an API without strict output
    choice = data.get("choice", "yes")
    if choice not in ("yes", "session", "saved"):
        raise ConsentError(f"no such choice: {choice!r}")
    if data["yes"]:
        verdict["choice"] = choice
    return verdict


# The grants an ask can offer beside its yes, as the consent check reads
# them
OFFERS = {
    "session": "yes, and {what} run without asking for the rest of this session",
    "saved": "yes, and {what} run without asking in every session from now on",
}


def chosen_answer(verdict, ask):
    """What a verdict on the user's words picks of the ask's answers: the
    grant the words ask for, if the ask offers it, else a plain yes; None
    when the words aren't a yes."""
    if verdict.get("yes") is not True:
        return None
    offered = ask.get("choices") or [YES, NO]
    choice = verdict.get("choice")
    return choice if choice in offered and choice != NO else YES


def question(ask):
    """The ask as the user saw it, from its `ask` record: with the grants it
    offers, if any."""
    asked = {"tool": ask["tool"], "command": ask["command"], "why": ask["why"]}
    what = ask.get("notes", {}).get("offer_text")
    offered = [c for c in ask.get("choices", ()) if c in OFFERS]
    if what and offered:
        asked["choices"] = {c: OFFERS[c].format(what=what) for c in offered}
    return asked


class Consent:
    """One model call per reply: JSON at temperature 0. `model`: the model
    it asks, as the model component makes it (its name, its API, its name at
    the server, its client, silence timeout and how to ask it for JSON)."""

    def __init__(self, model):
        self.model = model.name
        self.served_name = model.served_name
        # Seconds the check may stay silent (complete's `silence`)
        self.timeout = model.timeout
        self.client = model.client
        self.api = model.api
        # How to ask the model for JSON, as it honours it
        self.json_request = model.json_request
        # Prompt progress where the model honours it (model/features.py)
        self.progress = model.progress

    def message(self, asked, reply):
        return (
            "The question the user saw, as JSON:\n"
            f"{json.dumps(asked, ensure_ascii=False)}\n\n"
            "The user's reply, as a JSON string:\n"
            f"{json.dumps(reply, ensure_ascii=False)}"
        )

    def decide(self, asked, reply):
        """A dict: yes (True only for a plain yes), reason or error, seconds,
        tokens and the model's thinking when it shows it."""
        t0 = time.monotonic()
        messages = [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": self.message(asked, reply)},
        ]
        tokens = thought = None
        try:
            reply = complete(
                self.api, self.client, self.served_name, messages,
                silence=self.timeout, temperature=0,
                **self.json_request(SCHEMA), **self.progress(),
            )
            tokens = [reply.prompt_tokens, reply.cached_tokens,
                      reply.completion_tokens]
            thought = reply.reasoning
            verdict = parse(reply.content)
            # Only an offered grant counts; any other is a plain yes
            if verdict.get("choice") not in ("yes", *asked.get("choices", {})):
                verdict["choice"] = "yes"
        except Exception as e:
            # An API error, a bad verdict, anything: not a yes. The reply goes to
            # the agent as a message instead
            verdict = {"yes": False, "error": f"{type(e).__name__}: {e}"}
        verdict["seconds"] = round(time.monotonic() - t0, 1)
        verdict["model"] = self.model
        if tokens:
            verdict["tokens"] = tokens
        if thought:
            verdict["thinking"] = thought
        return verdict
