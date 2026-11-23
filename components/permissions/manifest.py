"""Permissions component manifest: modes, grants, hand-offs, and approvers."""

from dataclasses import replace

import paths
from core.components import ComponentManifest
from components.permissions import SETTINGS
from components.permissions.approval import (
    READ_ONLY,
    DefaultApproval,
    PermissionAsks,
    Refuse,
    UserApproval,
    ask_user,
)
from components.permissions.asks import Asks, AskWhen
from components.permissions.consent import Consent
from components.permissions.grants import GrantCheck, read_saved
from components.permissions.handoffs import HandoffApproval
from components.permissions.modes import (
    EVERY_CALL,
    JUDGED,
    MODES,
    UNSANDBOXED,
    answers_line,
    canonical,
    mode_rows,
    raised,
)
from components.permissions.service import PermissionsService


def _validate_mode(value):
    try:
        canonical(value)
    except ValueError:
        return f"mode: unknown mode {value!r}"
    return None


def _settings():
    return tuple(
        replace(setting, validate=_validate_mode)
        if setting.name == "mode"
        else setting
        for setting in SETTINGS
    )


def _unattended_check(build):
    checks = build.state.get("unattended_checks", ())

    def check(turn):
        return any(predicate(turn) for predicate in checks)

    return check


def _prepare_session(build):
    mode = canonical(build.mode)
    if build.runs_as and build.runs_as != build.mode:
        runs_as = canonical(build.runs_as)
    elif build.options.get("unattended"):
        runs_as = raised(mode)
    else:
        runs_as = canonical(build.runs_as or mode)
    build.mode = mode
    build.runs_as = runs_as
    spec = MODES[mode]
    runs_as_spec = MODES[runs_as]
    build.state["mode_spec"] = spec
    build.state["runs_as_spec"] = runs_as_spec
    build.state["judged"] = runs_as in JUDGED
    build.state["unsandboxed"] = mode in UNSANDBOXED
    build.state["answers_line"] = answers_line(mode, runs_as)
    build.settings["mode"] = mode
    if runs_as != mode:
        build.settings["runs_as"] = runs_as
    build.tool_context["retry_no_changes"] = runs_as == "auto+"


def _session_hooks(build):
    spec = build.state["runs_as_spec"]
    unattended = _unattended_check(build)
    hooks = [HandoffApproval(unattended, enabled=build.runs_as == "default")]
    if "saved_permissions" in build.options:
        saved = build.options["saved_permissions"]
    elif build.options.get("on_a_server"):
        saved = paths.permissions_file()
    else:
        saved = None
    # The permissions memory runs before RepeatGuard, which lets granted calls run.
    if spec.decides in ("user", "judge", EVERY_CALL):
        hooks.append(GrantCheck(saved, judged=build.runs_as in JUDGED))
    if spec.asks:
        hooks.append(Asks())
    ask_when = build.options.get("ask_when")
    if ask_when is not None and spec.decides is not None:
        hooks.append(AskWhen(ask_when))
    ask = build.ask
    if spec.decides == "nobody":
        hooks.append(Refuse(READ_ONLY))
    elif spec.decides == "user":
        user = UserApproval(ask) if ask is not None else Refuse()
        judge_hook = build.state.get("judge_approval")
        if judge_hook is not None:
            hooks.append(DefaultApproval(user, judge_hook, unattended))
        else:
            hooks.append(user)
    elif spec.decides in ("judge", EVERY_CALL):
        if spec.decides == EVERY_CALL:
            hooks.append(PermissionAsks())
    else:
        hooks.append(Asks(every_mode=True))
        hooks.append(UserApproval(ask) if ask is not None else Refuse())
    return hooks


def _after_session(session, build):
    # A fork's log opens with its parent's header; keep this session's own approver.
    if "runs_as" in build.settings:
        session.settings = {**session.settings, "runs_as": build.runs_as}
    else:
        updated = dict(session.settings)
        updated.pop("runs_as", None)
        session.settings = updated


def _service(build):
    saved = build.options.get("saved_permissions", paths.permissions_file)
    make_consent = build.options.get("make_consent")
    if make_consent is None:
        def silence():
            return build.limits["consent_silence_seconds"]

        def make_consent(model):
            models = build.services.get("models") or build.services.get("model")
            return Consent(models.make_model(model, timeout=silence))

    return PermissionsService(make_consent, saved)


def _doctor(workspace=None):
    notes = []
    read_saved(paths.permissions_file(), notes=notes)
    return [("!", note) for note in notes]


MANIFEST = ComponentManifest(
    name="permissions",
    order=10,
    settings=_settings(),
    system_sources=("modes.py",),
    prepare_session=_prepare_session,
    session_hooks=_session_hooks,
    after_session=_after_session,
    service=_service,
    doctor=_doctor,
)
