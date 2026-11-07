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


# Least to most permissive
MODES = {
    "readOnly": Mode("read-only", True, "nobody"),
    "default": Mode("workspace", True, "user"),
    "auto": Mode("workspace", True, "judge"),
    "auto+": Mode(None, False, "every call"),
    "bypassPermissions": Mode(None, False, None),
}
# Who answers a mode's asks, as the session's environment says it
ANSWERS = {
    "nobody": "a call that needs permission is refused",
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


def answers_line(mode):
    """The mode and who answers its asks: the environment's line."""
    return f"mode: {mode}; {ANSWERS[MODES[canonical(mode)].decides]}"
