"""Waiting: a turn held open until something comes for it (6.8.1).

tool.py: `wait`, which holds the turn until a notice or a message from the
user comes, or its minutes pass. Without it a turn that had started
something and wanted its result had two ways: end with a reply the user
reads and be started again by the notice, or hold itself open with
`sleep`, a command that blocks the very notice it waits for and becomes a
job of its own after the shell's foreground time (20 of them in one real
task, a third of its time). No settings of its own.
"""
