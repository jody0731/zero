"""Jobs component manifest: background command caps, notices, and JobsService."""

from core.components import ComponentManifest
from components.jobs import SETTINGS
from components.jobs.hooks import JobLimit, RunningJobs
from components.jobs.service import JobsService


def _session_hooks(build):
    spec = build.state.get("runs_as_spec")
    hooks = []
    if spec is None or spec.decides is not None:
        hooks.append(JobLimit())
    hooks.append(RunningJobs())
    return hooks


def _service(build):
    tick = build.options.get("tick")
    return JobsService(tick=2.0 if tick is None else tick)


MANIFEST = ComponentManifest(
    name="jobs",
    order=72,
    settings=SETTINGS,
    session_hooks=_session_hooks,
    service=_service,
)
