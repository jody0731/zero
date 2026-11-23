"""Judge component manifest: automated approval model and hooks."""

from collections.abc import Mapping

from core import entries
from core.components import ComponentManifest
from components.judge import SETTINGS
from components.judge.hooks import JudgeApproval
from components.judge.verdict import Judge
from components.model.servers import ModelServerError, resolve

# Conservative default matching context/room.py when no override is supplied.
DEFAULT_BYTES_PER_TOKEN = 1


def logged_judge(log):
    """Return the judge settings recorded in `log`, including legacy dicts."""
    head = log.first_record()
    if head is None or not entries.has(head.entry, "header"):
        return None
    logged = log.settings_now()
    if "judge_model" in logged or "judge_thinking_effort" in logged:
        return {
            key: logged[key]
            for key in ("judge_model", "judge_thinking_effort")
            if key in logged
        }
    if isinstance(legacy := logged.get("judge"), Mapping):
        return {
            "judge_model": legacy.get("model"),
            "judge_thinking_effort": legacy.get("effort"),
        }
    return None


class LazyJudge:
    """A judge made at its first use: default's, which answers only in a
    round, so a session that never works in rounds never makes one."""

    def __init__(self, make):
        self._make = make
        self._judge = None

    def __getattr__(self, name):
        if self._judge is None:
            self._judge = self._make()
        return getattr(self._judge, name)


def _prepare_session(build):
    legacy = logged_judge(build.log) if build.log is not None else None
    info = build.options.get("_info") or {}
    if "judge_model" in build.options:
        judge_model = build.options["judge_model"]
    elif "judge_model" in info:
        judge_model = info["judge_model"]
    elif legacy is not None and "judge_model" in legacy:
        judge_model = legacy["judge_model"]
    else:
        judge_model = build.settings.get("judge_model")

    if "judge_thinking_effort" in build.options:
        judge_thinking_effort = build.options["judge_thinking_effort"]
    elif "judge_thinking_effort" in info and info["judge_thinking_effort"] is not None:
        judge_thinking_effort = info["judge_thinking_effort"]
    elif legacy is not None and "judge_thinking_effort" in legacy:
        judge_thinking_effort = legacy["judge_thinking_effort"]
    else:
        judge_thinking_effort = build.settings.get("judge_thinking_effort")

    judged = bool(build.state.get("judged"))
    spec = build.state.get("runs_as_spec")
    needs_judge = (
        judged
        or (spec is not None and spec.decides == "user")
        or build.runs_as == "default"
    )
    judge = None
    if needs_judge:
        fallback_name = build.state.get("fallback_name") or (lambda: None)
        judge_client = build.options.get("judge_client")
        fallback_client = build.options.get("fallback_client")
        bytes_per_token = build.state.get(
            "bytes_per_token", DEFAULT_BYTES_PER_TOKEN,
        )
        keywords = (
            build.state.get("user_keywords")
            or build.state.get("keywords")
            or (lambda: ())
        )

        def make_judge(name, client, fallback=None):
            judge_server, _ = resolve(name, check_endpoint=client is None)
            input_tokens = min(
                judge_server.context_window,
                judge_server.max_input_tokens or judge_server.context_window,
            )
            return Judge(
                name,
                client,
                limits=build.limits,
                effort=judge_thinking_effort,
                view_bytes=input_tokens // 2 * bytes_per_token,
                fallback=fallback,
                keywords=keywords,
            )

        def spare_judge():
            name = fallback_name()
            if not name or name == (judge_model or build.model):
                return None
            try:
                return make_judge(name, fallback_client)
            except ModelServerError:
                return None

        def the_judge():
            return make_judge(
                judge_model or build.model, judge_client, spare_judge,
            )

        judge = the_judge() if judged else LazyJudge(the_judge)
        build.state["judge"] = judge
        build.state["judge_approval"] = JudgeApproval(judge)
    build.settings["judge_model"] = judge_model
    build.settings["judge_thinking_effort"] = judge_thinking_effort
    build.settings["judge"] = (
        {"model": judge.model, "effort": judge.effort} if judged else None
    )


def _session_hooks(build):
    if build.state.get("judged"):
        hook = build.state.get("judge_approval")
        if hook is not None:
            return [hook]
    return []


def _after_session(session, build):
    # A fork's log opens with its parent's header; keep this session's judge.
    session.settings = {
        **session.settings,
        "judge": build.settings.get("judge"),
    }


MANIFEST = ComponentManifest(
    name="judge",
    order=90,
    settings=SETTINGS,
    prepare_session=_prepare_session,
    session_hooks=_session_hooks,
    after_session=_after_session,
)
