"""The modes: each a sandbox and who decides an ask, least to most
permissive. Adding, removing or replacing a mode is one row of MODES; what each
loads is the assembly's.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Mode:
    # "read-only", "workspace", or None for no sandbox
    sandbox: str | None
    # Whether the sandbox's asks apply (leaving it, a local fetch)
    asks: bool
    # Who decides an ask: "nobody" (refused), "user", "judge", "every call"
    # (the judge reviews each call that needs permission), or None (nothing
    # asks)
    decides: str | None
    # What it is, in a line, for the user (apps show it: the server sends it)
    about: str = ""


# Least to most permissive
MODES = {
    "readOnly": Mode("read-only", True, "nobody",
                     "nothing writable; asks refused"),
    "default": Mode("workspace", True, "user",
                    "the workspace writable in the sandbox; you answer asks"),
    "auto": Mode("workspace", True, "judge",
                 "the workspace writable in the sandbox; a judge answers asks"),
    "auto+": Mode(None, False, "every call",
                  "no sandbox; the judge reviews every call that needs "
                  "permission"),
    "bypassPermissions": Mode(None, False, None, "no sandbox; nothing asked"),
}
# Who answers a mode's asks, as the session's environment says it
ANSWERS = {
    # The tools say a call goes first to the approver the mode line names
    # (core/asking.py's TO_APPROVER); readOnly names no one
    "nobody": "no one approves, so a call that goes to the approver is refused",
    "user": "the user answers each ask",
    "judge": "the judge answers each ask, and the user isn't asked",
    "every call": "the judge reviews each call that needs permission, and the "
                  "user isn't asked",
    None: "nothing asks but a change to your own files, which the user answers",
}
JUDGED = tuple(n for n, m in MODES.items() if m.decides in ("judge", "every call"))
UNSANDBOXED = tuple(n for n, m in MODES.items() if m.sandbox is None)


def canonical(mode):
    """The mode, checked; ValueError if unknown."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; modes: {tuple(MODES)}")
    return mode


def raised(mode):
    """The mode a session nobody watches runs as (a subagent ZERO started):
    default as auto, the sandbox kept and the judge answering; every other
    mode as itself, readOnly never raised."""
    return "auto" if canonical(mode) == "default" else mode


def answers_line(mode, runs_as=None):
    """The mode and who answers its asks: the environment's line. `runs_as`:
    the mode whose approver answers, where nobody watches (raised)."""
    decides = MODES[canonical(runs_as or mode)].decides
    return f"mode: {mode}; {ANSWERS[decides]}"


def mode_rows():
    """Every mode as apps read it (protocol/messages.py's row.mode), least to
    most permissive. A key that steps through them skips the one where
    nothing asks: it is chosen by its name, so no key lands on it by
    accident."""
    return [{"name": name, "about": mode.about,
             "in_cycle": mode.decides is not None}
            for name, mode in MODES.items()]
