"""The prompt: what the model is told when a session starts, in order.

SOUL.md and ZERO.md: the templates of the user's two files (who ZERO is; the
user, their machines, their instructions and ZERO's memory).
instructions.py: reads them and the project's ZERO.md files, and starts the
system prompt with them and the loop's rule.
hooks.py: whose pick the means of carrying out a task are, with the user's
trade between waiting and weighted tokens (the setting below), then the
environment (the user, the workspace, the box, the system, the date).
"""

from settings import Setting

# The user's trade between waiting and weighted tokens, which the system
# message states where it says the means are ZERO's pick (instructions.py,
# TRADES): the one fact a pick between two means that both do the task
# lacks, and the user's to give
TIME_OR_TOKENS = ("fewest_tokens", "balanced", "least_waiting")

SETTINGS = (
    Setting("time_or_tokens", "Default", "Time or tokens", "balanced",
            "choice", choices=TIME_OR_TOKENS),
)
