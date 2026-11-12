"""The MCP servers the user listed, in ZERO's home (paths.mcp_servers_file),
a table each, by the name its tools are offered under:

    file_format = 1

    [rea]
    command = "/home/me/.local/bin/rea"
    args = ["mcp"]
    call_seconds = 450

    [rea.env]
    JAVA_HOME = "/usr/lib/jvm/java-21-openjdk-amd64"

    [docs]
    url = "https://example.org/mcp"

    [docs.headers]
    Authorization = "Bearer …"

A server runs as a command ZERO starts (stdio: `command`, `args`, `env`,
`cwd`) or is reached at a `url` (Streamable HTTP, with `headers`). `tools`
names the tools offered, every one without it (Codex's enabled_tools);
`start_seconds` and `call_seconds` override the settings for this server
(Codex's startup_timeout_sec and tool_timeout_sec). A command's environment
is the MCP SDK's short list (HOME, LOGNAME, PATH, SHELL, TERM, USER) and the
entry's `env`, never the whole of ZERO's (Codex's, Hermes' and OpenClaw's
way).

Read leniently: what this ZERO can't use is named in a note (`zero doctor`,
`zero mcp list`) and the rest used; an entry it can't use at all is left
out. Written by one writer at a time (paths.changing), whole (write_whole),
an entry added at the end and one removed by its tables, so the user's other
lines and comments stay as they are.

An entry may be copied from another agent's list (`zero mcp add NAME --from
claude|codex`): Claude Code's ~/.claude.json and Codex's ~/.codex/config.toml.
"""

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import paths

# mcp_servers.toml's format, a line at its top (paths.FILE_FORMAT_KEY)
FILE_FORMAT = 1
# A server's name: what its tools' names start with (DeepSeek Harness's
# form), so the model's tool names stay in the API's characters
NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
NAME_RULE = "1 to 32 letters, digits, - or _"
# What an entry may hold, and the kind of each
STRINGS, PAIRS, NUMBER, TEXT = "strings", "pairs", "number", "text"
FIELDS = {
    "command": TEXT, "args": STRINGS, "env": PAIRS, "cwd": TEXT,
    "url": TEXT, "headers": PAIRS, "tools": STRINGS,
    "start_seconds": NUMBER, "call_seconds": NUMBER,
}
WHERE = "mcp_servers.toml"


@dataclass(frozen=True)
class Entry:
    """One MCP server as listed: a command ZERO starts, or a url."""

    name: str
    command: str | None = None
    args: tuple = ()
    env: dict = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict = field(default_factory=dict)
    # The tools offered, by the server's names for them; None: every one
    tools: tuple | None = None
    start_seconds: float | None = None
    call_seconds: float | None = None

    @property
    def transport(self):
        """How ZERO reaches it: "stdio" (a command) or "http" (a url)."""
        return "stdio" if self.command else "http"

    def where(self):
        """The command and its arguments, or the url, in a line."""
        if self.command:
            return " ".join([self.command, *self.args])
        return self.url

    def table(self):
        """The entry as its table in the file holds it, unset fields left
        out."""
        found = {"command": self.command, "args": list(self.args) or None,
                 "env": dict(self.env) or None, "cwd": self.cwd,
                 "url": self.url, "headers": dict(self.headers) or None,
                 "tools": None if self.tools is None else list(self.tools),
                 "start_seconds": self.start_seconds,
                 "call_seconds": self.call_seconds}
        return {k: v for k, v in found.items() if v is not None}

    def shown(self):
        """The entry for a reply: its values, with those of `env` and
        `headers` left out (tokens sit there), their names kept."""
        table = self.table()
        for secret in ("env", "headers"):
            if secret in table:
                table[secret] = sorted(table[secret])
        return {"name": self.name, "transport": self.transport, **table}


@dataclass
class Listed:
    """The entries ZERO can use, in the file's order, and what it says of
    the rest (`notes`, a line each)."""

    entries: list
    notes: list

    def named(self, name):
        return next((e for e in self.entries if e.name == name), None)


def read(path=None):
    """The list as it stands; an empty one where there is no file."""
    path = path or paths.mcp_servers_file()
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return Listed([], [])
    except OSError as error:
        return Listed([], [f"{WHERE}: can't be read ({error})"])
    return parse(text)


def parse(text):
    """A Listed from the file's text."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        return Listed([], [f"{WHERE} isn't TOML ZERO can read ({error}): no "
                           "MCP server is started"])
    notes = []
    found = data.pop(paths.FILE_FORMAT_KEY, FILE_FORMAT)
    if isinstance(found, int) and found > FILE_FORMAT:
        notes.append(paths.newer_note(WHERE, found, FILE_FORMAT))
    entries = []
    for name, table in data.items():
        entry, said = entry_of(name, table)
        notes += said
        if entry is not None:
            entries.append(entry)
    return Listed(entries, notes)


def entry_of(name, table):
    """(the Entry, [notes]) for one table; None where it can't be used."""
    where = f"{WHERE}, [{name}]"
    if not isinstance(table, dict):
        return None, [f"{WHERE}: `{name}` isn't a table, left out"]
    if not NAME.match(name):
        return None, [f"{where}: its name isn't {NAME_RULE}, left out"]
    notes, values = [], {}
    unknown = [key for key in table if key not in FIELDS]
    if unknown:
        notes.append(paths.unknown_note(where, unknown))
    for key, kind in FIELDS.items():
        if key not in table:
            continue
        value = checked(table[key], kind)
        if value is None:
            notes.append(f"{where}: `{key}` isn't {kind_words(kind)}, left out")
            if key in ("command", "url"):
                return None, notes
            continue
        values[key] = value
    if bool(values.get("command")) == bool(values.get("url")):
        notes.append(f"{where}: it needs a command or a url, one of them; "
                     "left out")
        return None, notes
    return Entry(
        name=name, command=values.get("command"),
        args=tuple(values.get("args", ())), env=values.get("env", {}),
        cwd=values.get("cwd"), url=values.get("url"),
        headers=values.get("headers", {}),
        tools=tuple(values["tools"]) if "tools" in values else None,
        start_seconds=values.get("start_seconds"),
        call_seconds=values.get("call_seconds"),
    ), notes


def checked(value, kind):
    """The value as its kind takes it, or None."""
    if kind == TEXT:
        return value if isinstance(value, str) and value.strip() else None
    if kind == STRINGS:
        ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
        return list(value) if ok else None
    if kind == PAIRS:
        ok = isinstance(value, dict) and all(
            isinstance(v, str) for v in value.values())
        return dict(value) if ok else None
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value) if ok and value > 0 else None


def kind_words(kind):
    return {TEXT: "a string", STRINGS: "a list of strings",
            PAIRS: "a table of strings", NUMBER: "a number above 0"}[kind]


def toml_value(value):
    """A value as TOML writes it: a JSON string or list of strings is a TOML
    one, a number as itself."""
    if isinstance(value, (str, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return repr(value)


def entry_text(entry):
    """The entry's tables, as add writes them."""
    table = entry.table()
    lines = [f"[{entry.name}]"]
    lines += [f"{key} = {toml_value(value)}" for key, value in table.items()
              if not isinstance(value, dict)]
    for key, pairs in table.items():
        if isinstance(pairs, dict):
            lines.append(f"\n[{entry.name}.{key}]")
            lines += [f"{json.dumps(k)} = {toml_value(v)}"
                      for k, v in pairs.items()]
    return "\n".join(lines) + "\n"


def add(entry, path=None):
    """List a new server, at the end; ValueError for a name already listed
    or a file ZERO can't read."""
    path = Path(path or paths.mcp_servers_file())
    with paths.changing(path):
        text = path.read_text() if path.exists() else ""
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise ValueError(f"{WHERE} isn't TOML ZERO can read ({error}); "
                             "fix it first") from None
        if entry.name in data:
            raise ValueError(f"an MCP server named {entry.name} is listed "
                             "already; remove it first")
        if not text.strip():
            text = f"{paths.FILE_FORMAT_KEY} = {FILE_FORMAT}\n"
        text = text.rstrip("\n") + "\n\n" + entry_text(entry)
        paths.write_whole(path, text, mode=0o600)


def remove(name, path=None):
    """Take a server off the list, its tables only; LookupError where it
    isn't listed."""
    path = Path(path or paths.mcp_servers_file())
    with paths.changing(path):
        try:
            text = path.read_text()
        except FileNotFoundError:
            raise LookupError(f"no MCP server named {name} is listed") from None
        kept, found = [], False
        for header, piece in pieces(text):
            if header is not None and table_name(header) == name:
                found = True
                continue
            kept.append(piece)
        if not found:
            raise LookupError(f"no MCP server named {name} is listed")
        paths.write_whole(path, "".join(kept).rstrip("\n") + "\n")


def pieces(text):
    """The file's text in pieces, each a table with the lines under it: the
    top-level lines first (header None)."""
    found, header, lines = [], None, []
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("["):
            found.append((header, "".join(lines)))
            header, lines = line.strip(), []
        lines.append(line)
    found.append((header, "".join(lines)))
    return found


def table_name(header):
    """The server a table's header belongs to: `[rea]` and `[rea.env]`
    both rea's."""
    inside = header.strip("[] ")
    return inside.split(".", 1)[0].strip().strip('"')


# ------------------------------------------------------ other agents' lists


def claude_file():
    """Claude Code's own file, whose `mcpServers` are the user's servers."""
    return Path.home() / ".claude.json"


def codex_file():
    return Path.home() / ".codex" / "config.toml"


def from_claude(name, path=None):
    """(the Entry, [notes]) copied from Claude Code's user list."""
    path = Path(path or claude_file())
    try:
        servers = json.loads(path.read_text()).get("mcpServers") or {}
    except (OSError, ValueError) as error:
        raise LookupError(f"Claude Code's list, {path}, can't be read "
                          f"({error})") from None
    table = servers.get(name)
    if not isinstance(table, dict):
        raise LookupError(not_there("Claude Code's", path, name, servers))
    notes = []
    if table.get("type") == "sse":
        notes.append("Claude Code reaches it over SSE, which ZERO doesn't "
                     "speak; its url is tried as Streamable HTTP")
    copied = {key: table[key] for key in ("command", "args", "env", "url",
                                          "headers", "cwd") if key in table}
    return copy_of(name, copied, notes)


def from_codex(name, path=None):
    """(the Entry, [notes]) copied from Codex's config.toml."""
    path = Path(path or codex_file())
    try:
        servers = tomllib.loads(path.read_text()).get("mcp_servers") or {}
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise LookupError(f"Codex's list, {path}, can't be read ({error})"
                          ) from None
    table = servers.get(name)
    if not isinstance(table, dict):
        raise LookupError(not_there("Codex's", path, name, servers))
    renamed = {"http_headers": "headers", "startup_timeout_sec": "start_seconds",
               "tool_timeout_sec": "call_seconds", "enabled_tools": "tools"}
    copied = {renamed.get(key, key): value for key, value in table.items()
              if key in FIELDS or key in renamed}
    notes = []
    for key in ("env_vars", "bearer_token_env_var", "env_http_headers"):
        if key in table:
            notes.append(f"Codex's `{key}` isn't copied: ZERO passes only the "
                         "values the entry holds")
    if table.get("enabled") is False:
        notes.append("Codex has it turned off")
    return copy_of(name, copied, notes)


AGENTS = {"claude": from_claude, "codex": from_codex}


def not_there(whose, path, name, servers):
    names = ", ".join(sorted(servers)) or "none"
    return (f"{whose} list, {path}, has no MCP server named {name} (it has: "
            f"{names})")


def copy_of(name, table, notes):
    entry, said = entry_of(name, table)
    if entry is None:
        raise LookupError("; ".join(said) or f"{name} can't be copied")
    return entry, notes + said


def from_agent(agent, name):
    """(the Entry, [notes]) copied from `agent`'s list ("claude", "codex");
    LookupError where it isn't there or can't be read."""
    if agent not in AGENTS:
        raise LookupError(f"ZERO copies from {', '.join(AGENTS)}, not {agent!r}")
    if not NAME.match(name):
        raise LookupError(f"an MCP server's name in ZERO is {NAME_RULE}")
    return AGENTS[agent](name)
