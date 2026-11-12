"""`zero doctor`: what ZERO needs from this machine, each need checked by
trying it (design rule 8; the checks are the components', declared to the
assembly), then what ZERO's files say that this ZERO doesn't know. The command
line runs this in its own place, as it runs the server, so it never loads the
harness itself; install.sh runs it once ZERO is in place. Exit code 0 when
ZERO can run here, 1 when a need isn't met.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import assembly  # noqa: E402
import paths  # noqa: E402

MET, UNMET, NOTE = "✓", "✗", "!"


def home_line():
    """ZERO's home: where it is, and whether ZERO can write there."""
    home = paths.home()
    there = home if home.exists() else home.parent
    if not os.access(there, os.W_OK):
        return UNMET, f"ZERO's home, {home}: ZERO can't write there"
    return MET, f"ZERO's home: {home}"


def report():
    """[(mark, line)]: the machine's needs, ZERO's home, its files' notes."""
    version = ".".join(str(n) for n in sys.version_info[:3])
    lines = [(MET, f"Python {version}")]
    for need, check in assembly.MACHINE_CHECKS.items():
        found = check()
        lines.append((MET, need) if found is None
                     else (UNMET, f"{need}: no; {found}"))
    lines.append(home_line())
    lines += [(NOTE, note) for note in assembly.home_file_notes()]
    return lines


def main(out=print):
    lines = report()
    for mark, line in lines:
        out(f"{mark} {line}")
    if any(mark == UNMET for mark, _ in lines):
        out("ZERO can't run here as it is: fix what is marked ✗ (README, "
            "What it needs), then zero doctor again.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
