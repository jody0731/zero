"""The session's results folder: a result or an output saved whole when it
doesn't stay in the context, named for the record it came from.

The folder sits beside the session's log, `results/<session>/`, so it
outlives a restart (the scratch folder doesn't: a resumed session would
find nothing at the path it was told) and goes to the trash with the log.
The sandbox shows it read-only at its own path, so `read` and the shell
reach it as ZERO was told.
"""

from pathlib import Path


def folder_of(log_path):
    """A session's results folder, from its log's path."""
    log_path = Path(log_path)
    return log_path.parent / "results" / log_path.stem


def save(folder, name, text):
    """`text` saved whole as `name` in `folder`, readable by the user only;
    its path."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def lines_of(text):
    """How many lines `text` has, a last one without a newline counted."""
    return text.count("\n") + (0 if not text or text.endswith("\n") else 1)
