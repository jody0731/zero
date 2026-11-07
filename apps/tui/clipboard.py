"""Copying to the desktop's clipboard.

As Claude Code and Codex do: the desktop's own tool first (wl-copy on Wayland,
xclip or xsel on X11), and only when none is there or it fails, OSC 52, which
asks the terminal to set the clipboard and gets no answer back (Codex's
clipboard_copy.rs; Claude Code's fullscreen docs). Ctrl+V reads the clipboard
through the same tools, as in a desktop app; the terminal's own paste works
too.
"""

import os
import shutil
import subprocess

# What `copy` says when only the terminal was asked
TERMINAL = "the terminal (OSC 52)"
# A tool that hangs gives up after this many seconds
TIMEOUT_SECONDS = 3


def tools(env):
    """The clipboard tools to try, in order, for this desktop."""
    found = []
    if env.get("WAYLAND_DISPLAY"):
        found.append(["wl-copy"])
    if env.get("DISPLAY"):
        found += [["xclip", "-selection", "clipboard"],
                  ["xsel", "--clipboard", "--input"]]
    return found


# What reads the clipboard, by the tool that writes it
READERS = {
    "wl-copy": ["wl-paste", "--no-newline"],
    "xclip": ["xclip", "-selection", "clipboard", "-o"],
    "xsel": ["xsel", "--clipboard", "--output"],
}


def copy(text, osc52, env=None, which=shutil.which, run=subprocess.run):
    """Put `text` on the clipboard; return what took it. `osc52(text)` asks
    the terminal, when no tool did."""
    for argv in tools(os.environ if env is None else env):
        if which(argv[0]) is None:
            continue
        try:
            # wl-copy stays behind to serve the clipboard: no pipes held open
            done = run(argv, input=text.encode(), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            return argv[0]
    osc52(text)
    return TERMINAL


def paste(env=None, which=shutil.which, run=subprocess.run):
    """The clipboard's text, or None when no tool reads it or it's empty."""
    for writer in tools(os.environ if env is None else env):
        argv = READERS[writer[0]]
        if which(argv[0]) is None:
            continue
        try:
            done = run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       timeout=TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0 and done.stdout:
            return done.stdout.decode(errors="replace")
    return None
