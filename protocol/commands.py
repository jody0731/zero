"""Conversation commands shared by the terminal and browser apps."""

COMMANDS = {
    "/new": "a new session (asks for its workspace)",
    "/continue": "go on with a turn that ended without an answer",
    "/sessions": "the session list",
    "/rename": "rename this session: /rename TITLE",
    "/compact": "compact this session's context: now, or before the next step "
                "while a turn runs; /compact INSTRUCTIONS says what its handoff keeps",
    "/rewind": "go back to before one of your messages, its text back in the "
               "input; what came after is left behind, files stay as they are",
    "/fork": "a new session from this conversation, up to one of your messages "
             "or all of it",
    "/archive": "archive this session (or bring it back)",
    "/delete": "move this session to the trash (asked first)",
    "/jobs": "this session's background jobs",
    "/copy": "copy ZERO's last answer, as its Markdown",
    "/permissions": "what runs without asking (a plan's calls aside); "
                    "/permissions forget N takes one back",
    "/mode": "the mode, from the next turn: /mode NAME, or pick",
    "/model": "the model and its thinking, from the next turn: /model NAME, "
              "or pick",
    "/cd": "the folder its commands run in, from the next turn: /cd FOLDER",
    "/settings": "this session, new sessions' defaults, models and "
                 "keys, key bindings, Advanced",
    "/init": "ZERO looks over you and this machine and fills in its ZERO.md",
    "/help": "keys and commands",
    "/quit": "leave; turns and jobs go on in ZERO",
}
INIT = (
    "Get to know me and this machine: look around (who I am, from what's here; "
    "the hardware, the system, the main tools and services I use) and add what "
    "you find to the “The user” and “Machines” sections of my "
    "ZERO.md with the memory tool, one short entry per fact."
)
