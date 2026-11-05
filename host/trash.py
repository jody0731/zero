"""Delete to the trash (Pi): a deleted session's files are moved, never removed.

The desktop's trash through `gio trash` where it answers, so the files can be
restored from the file manager; otherwise ZERO's own `trash/` folder in its
data folder. Tests set GIO to None so nothing reaches the user's trash.
"""

import shutil
import subprocess
import time

GIO = shutil.which("gio")


def move(paths, files):
    """Move each file that exists to the trash; returns where they went."""
    files = [path for path in files if path.exists()]
    if not files:
        return None
    if GIO is not None:
        try:
            done = subprocess.run(
                [GIO, "trash", "--", *map(str, files)],
                capture_output=True, timeout=10,
            )
            if done.returncode == 0:
                return "the desktop's trash"
        except (OSError, subprocess.TimeoutExpired):
            pass
    folder = paths.trash / time.strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    for path in files:
        if path.exists():
            path.rename(folder / path.name)
    return str(folder)
