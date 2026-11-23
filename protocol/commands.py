"""Conversation commands shared by the terminal and browser apps."""

import json
from pathlib import Path

from protocol.messages import protocol_files

BASE_COMMANDS = {
    "/new": "a new session (asks for its workspace)",
    "/continue": "go on with a turn that ended without an answer",
    "/sessions": "the session list",
    "/rename": "rename this session: /rename TITLE",
    "/rewind": "go back to before one of your messages, its text back in the "
               "input; what came after is left behind, files stay as they are",
    "/fork": "a new session from this conversation, up to one of your messages "
             "or all of it",
    "/archive": "archive this session (or bring it back)",
    "/delete": "move this session to the trash (asked first)",
    "/copy": "copy ZERO's last answer, as its Markdown",
    "/settings": "this session, new sessions' defaults, models and "
                 "keys, key bindings, Advanced",
    "/help": "keys and commands",
    "/quit": "leave; turns and jobs go on in ZERO",
}


def load_commands(root, base=None):
    """Merge `components/*/protocol.json` commands before `/quit`."""
    commands = dict(BASE_COMMANDS if base is None else base)
    has_quit = "/quit" in commands
    quit_desc = commands.pop("/quit", None)
    extra = {}
    for path in protocol_files(root):
        data = json.loads(path.read_text(encoding="utf-8"))
        for name, about in (data.get("commands") or {}).items():
            extra[name] = about
    for name in sorted(extra):
        commands[name] = extra[name]
    if has_quit:
        commands["/quit"] = quit_desc
    return commands


COMMANDS = load_commands(Path(__file__).resolve().parent.parent)
INIT = (
    "Get to know me and this machine: look around (who I am, from what's here; "
    "the hardware, the system, the main tools and services I use) and add what "
    "you find to the “The user” and “Machines” sections of my "
    "ZERO.md with the memory tool, one short entry per fact."
)
