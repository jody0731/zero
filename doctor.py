"""`zero doctor`: what ZERO needs from this machine, each need checked by
trying it (design rule 8; the checks are the components', declared to the
assembly), then what ZERO's files say that this ZERO doesn't know, what
of the skills a session in this folder would find ZERO can't use as it is,
and what of the MCP servers listed it can't (a command not found here);
last, ZERO's HTTP door and the lines that point other agents at it.
The command line runs this in its own place, as it runs the server, so it
never loads the harness itself; install.sh runs it once ZERO is in place.
Exit code 0 when ZERO can run here, 1 when a need isn't met.
"""

import os
import subprocess
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


def python_lines():
    """The Python ZERO runs on, whose it is, and whether its threads run side
    by side: a free-threaded build runs sessions on every core; with the GIL
    they take turns on one, which works, slower under many sessions."""
    version = ".".join(str(n) for n in sys.version_info[:3])
    whose = ", ZERO's own" if paths.release_folder() else ""
    if not getattr(sys, "_is_gil_enabled", lambda: True)():
        return [(MET, f"Python {version}{whose}, free-threaded: sessions run "
                      "side by side")]
    return [(MET, f"Python {version}{whose}"),
            (NOTE, "Python's GIL is on: ZERO's sessions take turns on one core; "
                   "ZERO's releases bring their own free-threaded Python, which "
                   "runs them side by side: zero update, or install.sh (README, "
                   "Install)")]


def http_door_lines(run=subprocess.run):
    """ZERO's HTTP door: whether systemd holds its port, and the lines that
    point Claude Code and Codex at it (ZERO writes no other agent's
    settings: the user runs them)."""
    found = paths.Paths()
    url = f"http://127.0.0.1:{found.http_door_port}/mcp"
    unit = f"{found.http_door_unit}.socket"
    try:
        result = run(["systemctl", "--user", "is-active", unit],
                     capture_output=True, text=True, timeout=5)
        held = result.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        held = False
    agents = (f"Claude Code: claude mcp add --transport http --scope user "
              f"zero {url} (an entry that runs zero mcp serve goes first: "
              f"claude mcp remove zero --scope user); Codex: codex mcp add "
              f"zero --url {url}")
    if held:
        return [(MET, f"ZERO's HTTP door: {url}, held by systemd ({unit}). "
                      f"Other agents reach ZERO there; {agents}")]
    return [(NOTE, f"ZERO's HTTP door, {url}, isn't held by systemd: zero "
                   f"http-door install. Then other agents reach ZERO there; "
                   f"{agents}")]


def report():
    """[(mark, line)]: the machine's needs, ZERO's home, its files' notes."""
    lines = python_lines()
    for need, check in assembly.MACHINE_CHECKS.items():
        found = check()
        lines.append((MET, need) if found is None
                     else (UNMET, f"{need}: no; {found}"))
    lines.append(home_line())
    lines += [(NOTE, note) for note in assembly.home_file_notes()]
    # The skills a session here would find, as the folder it runs in says
    lines += [(NOTE, note) for note in assembly.skill_notes(Path.cwd())]
    # The MCP servers listed: what ZERO can't use, a command not found
    lines += [(NOTE, note) for note in assembly.mcp_server_notes()]
    lines += http_door_lines()
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
