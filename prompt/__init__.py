"""The prompt: what the model is told when a session starts, in order.

SOUL.md and ZERO.md: the templates of the user's two files (who ZERO is; the
user, their machines, their instructions and ZERO's memory).
instructions.py: reads them and the project's ZERO.md files, and starts the
system prompt with them and the loop's rule.
plugin.py: the environment (the user, the workspace, the box, the system, the
date), after them.
"""
