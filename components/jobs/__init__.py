"""Background jobs: a command that outlives its call.

tool.py: the `job` tool and the texts the model reads of a job (its start, its
notices). process.py: a job's process (spawned from one long-lived thread, in
its own group, its output in files, stopped by group). hook.py: the cap on
running jobs (JobLimit). service.py: what a job is in the server (its row, its
notices, its check-ins). The shell starts jobs; jobs know nothing of it.
"""

from settings import Setting

SETTINGS = (
    Setting("max_jobs", "Limits", "Running jobs before another one asks", 10,
            "int"),
)
