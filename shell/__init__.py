"""The shell: bash, the model's way to run commands.

tool.py: the `bash` tool (a fresh shell per call, in the session's box, its own
process group, a timeout, moved to the background as a job where the session
has jobs). output.py: its output as the model reads it. commands.py: a command
line as the simple commands it runs (the grants' prefixes).
"""

from settings import Setting

SETTINGS = (
    Setting("bash_foreground_seconds", "Limits",
            "Seconds before a command moves to the background", 30, "number"),
    # Only memory bounds a command's output: past this, millions of tokens,
    # more than any context holds, its start and end are kept (output.py)
    Setting("bash_output_kept_chars", "Limits",
            "Characters of a command's output kept", 16_000_000, "int",
            minimum=1_000),
)
