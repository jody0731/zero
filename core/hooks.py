"""The hook names, which ones decide, and how hooks get called.

A hook is a class whose method names are hook names. At each hook the loop
calls only the hooks that have that method, in profile order.

- watch hooks: every hook is called; what it returns is ignored.
- collect hooks: every hook is called; the texts are joined in profile order.
- decide hooks: every hook is called (no stopping at the first answer), then
  any Deny or Stop wins, so hook order can never turn a denial into permission;
  then a Park, then the longest Retry. Nothing means "no opinion", never
  "allow".

A crash in a decide hook fails closed (Deny or Stop); a crash in a watch or
collect hook is only a warning. Either way it's kept in `errors` for the log.
"""

import traceback

WATCH = {"turn_start", "after_model", "after_tool", "turn_end"}
# switched: what a session runs with after the user changed it between
# turns, told to the model in a note; resumed: what the restart note adds,
# when a turn cut off by a stopped server goes on
COLLECT = {"system_prompt", "switched", "resumed"}
DECIDE = {"before_model", "model_error", "before_tool", "approve", "step_end"}
EVENTS = WATCH | COLLECT | DECIDE


class Hook:
    """Base class. Subclasses add methods named after hooks, nothing else."""

    def __repr__(self):
        return type(self).__name__


class Deny:
    """Don't run this tool call; the model gets `reason` as the result."""

    def __init__(self, reason):
        self.reason = reason


class Stop:
    """End the turn with this outcome."""

    def __init__(self, outcome, detail):
        self.outcome = outcome
        self.detail = detail


class Park:
    """Don't run this tool call yet: the turn waits on disk for the user's reply
   . `why` is what the user is shown; `about`, what the ask is about, a
    statement; `reason`, the judge's own, over the call's."""

    def __init__(self, why, about=None, reason=None):
        self.why = why
        self.about = about
        self.reason = reason


class Retry:
    """Retry the failed model call after `delay` seconds."""

    def __init__(self, delay):
        self.delay = delay


class HookRunner:
    def __init__(self, hooks):
        self.hooks = list(hooks)
        # Hook crashes since the loop last took them: (where, traceback)
        self.errors = []
        # A typo like `before_tools` would otherwise be silently never called,
        # so public methods must be hook names; helpers start with _
        for hook in self.hooks:
            public = [n for n in dir(type(hook)) if not n.startswith("_")]
            unknown = [n for n in public if n not in EVENTS]
            if unknown:
                raise TypeError(f"{hook!r}: not hook names: {unknown}")

    def take_errors(self):
        errors, self.errors = self.errors, []
        return errors

    def _each(self, name, *args):
        """Yield (hook, answer, crash) for every hook that joins this hook."""
        for hook in self.hooks:
            method = getattr(hook, name, None)
            if method is None:
                continue
            try:
                yield hook, method(*args), None
            except Exception as e:
                where = f"{hook!r}.{name}"
                self.errors.append({"where": where, "error": traceback.format_exc()})
                print(f"(warning: {where} crashed: {type(e).__name__}: {e})")
                yield hook, None, f"{where} crashed ({type(e).__name__}: {e})"

    def notify(self, name, *args):
        assert name in WATCH, name
        for _ in self._each(name, *args):
            pass

    def collect(self, name, *args):
        assert name in COLLECT, name
        return "".join(text for _, text, _ in self._each(name, *args) if text)

    def decide(self, name, *args):
        """Ask every hook; return the winning decision, or None."""
        assert name in DECIDE, name
        decisions = []
        for _, answer, crash in self._each(name, *args):
            if crash:
                answer = fail_closed(name, crash)
            if answer is not None:
                decisions.append(answer)
        for kind in (Deny, Stop, Park):
            for decision in decisions:
                if isinstance(decision, kind):
                    return decision
        retries = [d for d in decisions if isinstance(d, Retry)]
        if retries:
            return max(retries, key=lambda d: d.delay)
        return None


def fail_closed(name, crash):
    """What a crashed decider counts as: the safe answer for that hook."""
    if name in ("before_tool", "approve"):
        return Deny(f"Not run: a bug in the harness ({crash}).")
    if name in ("before_model", "step_end"):
        return Stop("hook_error", crash)
    # model_error: no Retry, so the turn gives up
    return None
