"""The shell: bash, the model's way to run commands.

tool.py: the `bash` tool (a fresh shell per call, in the session's box, its own
process group, a timeout, moved to the background as a job where the session
has jobs). output.py: its output as the model reads it. commands.py: a command
line as the simple commands it runs (the grants' prefixes).
"""

from settings import Setting

SETTINGS = (
    Setting("bash_timeout", "Limits",
            "Seconds before a command moves to the background", 30, "number"),
    Setting("output_limit", "Limits", "Characters kept of a command's output",
            4000, "int", least=100),
)
