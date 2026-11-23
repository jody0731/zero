"""A bash command line as the simple commands it runs, and the prefixes
"don't ask again" grants for it (the bash tool's Grant).

A command is split into its parts (at ; && || | & newlines and parentheses),
`bash -c` is looked inside, wrappers (`sudo`, `env`, `timeout` …) are looked
past, and VAR=x is skipped. A command that can't be parsed raises Unparseable,
and so does one with parts the split can't see: a substitution inside quotes,
a process substitution. Every doubt makes the verdict stricter: a grant may
miss a command it could have covered, never cover one it shouldn't.
"""

import shlex
from pathlib import Path

SHELLS = {"bash", "sh", "zsh", "dash"}
# Words that run the rest of the line as a command
WRAPPERS = {
    "env", "sudo", "doas", "nohup", "time", "exec", "command", "nice", "ionice",
    "timeout", "stdbuf", "xargs",
}
PUNCTUATION = ";&|()<>\n"
# How many leading words name what a command does, for programs with
# subcommands: OpenCode's ARITY (packages/core/src/shell/parse.ts, MIT), with a
# few of Linux's own added. Any other program is named by its first word
ARITY = {
    "apt": 2, "apt-get": 2, "aws": 3, "az": 3, "bazel": 2, "brew": 2, "bun": 2,
    "bun run": 3, "bun x": 3, "cargo": 2, "cargo add": 3, "cargo run": 3, "cdk": 2,
    "cf": 2, "cmake": 2, "composer": 2, "consul": 2, "consul kv": 3, "crictl": 2,
    "deno": 2, "deno task": 3, "docker": 2, "docker builder": 3,
    "docker compose": 3, "docker container": 3, "docker image": 3,
    "docker network": 3, "docker volume": 3, "doctl": 3, "eksctl": 2,
    "eksctl create": 3, "firebase": 2, "flatpak": 2, "flyctl": 2, "gcloud": 3,
    "gh": 3, "git": 2, "git config": 3, "git remote": 3, "git stash": 3, "go": 2,
    "gradle": 2, "helm": 2, "heroku": 2, "hostnamectl": 2, "hugo": 2, "ip": 2,
    "ip addr": 3, "ip link": 3, "ip netns": 3, "ip route": 3, "kind": 2,
    "kind create": 3, "kubectl": 2, "kubectl kustomize": 3, "kubectl rollout": 3,
    "kustomize": 2, "loginctl": 2, "make": 2, "mc": 2, "mc admin": 3, "minikube": 2,
    "mongosh": 2, "mvn": 2, "mysql": 2, "ng": 2, "nmcli": 2, "npm": 2,
    "npm exec": 3, "npm init": 3, "npm run": 3, "npm view": 3, "npx": 2, "nvm": 2,
    "nx": 2, "openssl": 2, "openssl req": 3, "openssl x509": 3, "pip": 2,
    "pipenv": 2, "pnpm": 2, "pnpm dlx": 3, "pnpm exec": 3, "pnpm run": 3,
    "podman": 2, "podman container": 3, "podman image": 3, "poetry": 2, "psql": 2,
    "pulumi": 2, "pulumi stack": 3, "pyenv": 2, "rake": 2, "rbenv": 2,
    "redis-cli": 2, "resolvectl": 2, "rustup": 2, "serverless": 2, "sfdx": 3,
    "skaffold": 2, "sls": 2, "snap": 2, "sst": 2, "swift": 2, "systemctl": 2,
    "terraform": 2, "terraform workspace": 3, "timedatectl": 2, "tmux": 2,
    "turbo": 2, "ufw": 2, "vault": 2, "vault auth": 3, "vault kv": 3, "vercel": 2,
    "volta": 2, "wp": 2, "yarn": 2, "yarn dlx": 3, "yarn run": 3,
}
MAX_DEPTH = 8


class Unparseable(Exception):
    pass


def split(command, depth=0):
    """A command line as a list of simple commands, each a list of words."""
    if depth > MAX_DEPTH:
        raise Unparseable("too deeply nested")
    if "`" in command:
        raise Unparseable("backticks")
    lexer = shlex.shlex(command, posix=True, punctuation_chars=PUNCTUATION)
    # Newlines end a command in bash, so they are separators, not spaces.
    # shlex takes a '#' inside a word as a comment, which bash doesn't
    # (`echo a#b; rm x` runs the rm), so nothing is a comment here: a real
    # comment's words only make the verdict stricter
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError as e:
        raise Unparseable(str(e)) from e
    parts, words, skip = [], [], False
    for token in tokens:
        # Inside quotes, a substitution is one word the split can't open
        if "$(" in token:
            raise Unparseable("a substitution inside quotes")
        if skip:
            skip = False
        elif token and all(c in PUNCTUATION for c in token):
            if ("<" in token or ">" in token) and "(" in token:
                raise Unparseable("a process substitution")
            if "<" in token or ">" in token:
                # The word after a redirect is a file, not a command
                skip = True
            else:
                parts.append(words)
                words = []
        else:
            words.append(token)
    parts.append(words)
    simple = []
    for words in parts:
        simple += unwrap(words, depth)
    return [w for w in simple if w]


def prefix(words):
    """The words that name what a simple command does: `git push`, `docker
    compose up`, `rm` (OpenCode's prefix()); never past an option. A program
    named by its path keeps it: `./ls` is a script, not `ls`."""
    named = [program(words[0])] + words[1:]
    for length in range(min(len(words), 3), 0, -1):
        arity = ARITY.get(" ".join(named[:length]))
        if arity is not None:
            break
    else:
        arity = 1
    taken = []
    for word in words[:arity]:
        if word.startswith("-"):
            break
        taken.append(word)
    return " ".join(taken)


def unwrap(words, depth):
    """Skip VAR=x; look past wrappers and inside `bash -c "..."`."""
    while words and "=" in words[0] and not words[0].startswith("="):
        words = words[1:]
    if not words:
        return []
    if program(words[0]) not in WRAPPERS:
        return command(words, depth)
    # Which word starts the wrapped command depends on the wrapper's options
    # (sudo -u x, nice -n 5), so every tail is checked: an extra match only
    # makes the verdict stricter, never looser
    tails = []
    for start in range(len(words)):
        tails += command(words[start:], depth)
    return tails


def command(words, depth):
    """One simple command, as written; `bash -c` opened up."""
    if program(words[0]) in SHELLS:
        for i, word in enumerate(words[1:], 1):
            if word.startswith("-") and "c" in word and i + 1 < len(words):
                return split(words[i + 1], depth + 1)
    return [words]


def program(word):
    """The program a command's first word runs, by its name: `/bin/sed` is
    sed. What it may do is judged by this name; its grant keeps the path.
    `.` (bash's source) has no name of its own, so it is itself."""
    return Path(word).name or word


INTERPRETERS = {
    "python", "python2", "python3", "node", "nodejs", "perl", "ruby", "php",
    "lua", "deno", "bun", "Rscript", "julia", "osascript", "pwsh",
    "powershell", "fish", "ksh", "tclsh", "awk", "gawk", "mawk", "nawk",
}
# Programs that run their arguments, or a file, as commands: another shell's
# line, another user's, another machine's, another namespace's
RUNNERS = {
    "eval", "source", ".", "builtin", "trap", "watch", "setsid", "su", "sg",
    "runuser", "pkexec", "ssh", "flock", "script", "unshare", "nsenter",
    "chroot", "parallel", "systemd-run", "strace", "ltrace", "gdb", "busybox",
    "firejail", "bwrap", "taskset", "chrt", "numactl", "unbuffer", "faketime",
    "fakeroot", "proot", "newgrp", "at", "batch", "dbus-launch",
}
# Programs whose prefix would cover anything they run
NEVER_GRANTED = SHELLS | WRAPPERS | INTERPRETERS | RUNNERS
# Prefixes that run whatever a project or an option says (Codex's banned
# suggestions): `git -C x …`, a bare `npm run`
WIDE = {"git", "npm run", "pnpm run", "yarn run", "bun run"}
# Shell words that open a block or a test: the command is the word after.
# A block's end only closes it (redirects may follow)
OPENERS = {"if", "then", "else", "elif", "while", "until", "do", "!", "{"}
CLOSERS = {"done", "fi", "}"}
# Words that start something other than a command (a loop's list, a case's
# patterns); a part that starts with an option or a number is a fragment the
# split left (`find . \( -name x \)`, `) 2>/dev/null`). No grant for these
NOT_COMMANDS = {"for", "case", "esac", "select", "function", "coproc"}
# Options with which a program acts on what it finds: it runs a command,
# deletes, or writes a file. A grant for the program doesn't cover them
ACTING = {
    "find": ("-exec", "-ok", "-delete", "-fprint", "-fls"),
    "sed": ("--in-place", "-f", "--file"),
    "tar": ("--to-command", "--checkpoint-action", "--use-compress-program",
            "-I", "--info-script", "-F", "--new-volume-script"),
    "rsync": ("-e", "--rsh", "--rsync-path"),
    "sort": ("--compress-program",),
}


def prefixes(command):
    """Each part's prefix, or None for a command no grant may cover."""
    try:
        parts = split(command)
    except Unparseable:
        return None
    found = set()
    for words in parts:
        while words and words[0] in OPENERS:
            words = words[1:]
        if not words or words[0] in CLOSERS:
            continue
        first = words[0]
        if first in NOT_COMMANDS or first.startswith("-") or first.isdigit():
            return None
        # A comment line, or a program named by a variable (it runs whatever
        # the variable holds)
        if first.startswith("#") or "$" in first:
            return None
        if program(first) in NEVER_GRANTED or acts(words):
            return None
        found.add(prefix(words))
    if not found:
        return None
    # A wide program named by its path is as wide
    named = {" ".join([program(p.split()[0])] + p.split()[1:]) for p in found}
    if named & WIDE:
        return None
    return sorted(found)


def acts(words):
    """Whether a simple command acts through its options (ACTING); for sed,
    an -i or -f among its short options (`-i`, `-i.bak`, `-ni`) too, or a
    script that runs a command or writes a file."""
    name = program(words[0])
    options = ACTING.get(name, ())
    for word in words[1:]:
        if options and word.startswith(options):
            return True
        short = word.startswith("-") and not word.startswith("--")
        if name == "sed" and short and ("i" in word or "f" in word):
            return True
    return name == "sed" and any(sed_acts(s) for s in sed_scripts(words))


def sed_scripts(words):
    """The scripts a sed command runs: each -e's, else its first operand."""
    scripts, operands, rest = [], [], iter(words[1:])
    for word in rest:
        if word in ("-e", "--expression"):
            scripts.append(next(rest, ""))
        elif word.startswith("--expression="):
            scripts.append(word.split("=", 1)[1])
        elif word.startswith("-e") and len(word) > 2:
            scripts.append(word[2:])
        elif word in ("-l", "--line-length"):
            next(rest, None)
        elif not word.startswith("-"):
            operands.append(word)
    return scripts or operands[:1]


# sed's commands that run a command (e) or write a file (w, W); the s
# command's flags of the same letters do the same
SED_ACTING = set("ewW")
# What separates commands, and what an address is made of
SED_SKIPPED = set("; \t\n{}0123456789$,!~+")
# Commands that take the rest of their line: text to add, a file to read, a
# label, a comment
SED_TO_LINE_END = set("aicrRbtTlq:#")


def sed_acts(script):
    """Whether a sed script runs a command or writes a file; a script this
    can't read through counts as one that does."""
    i, n = 0, len(script)
    try:
        while i < n:
            c = script[i]
            if c in SED_SKIPPED:
                i += 1
            elif c == "/":
                i = sed_past(script, i, "/")
            elif c == "\\":
                i = sed_past(script, i + 1, script[i + 1])
            elif c in SED_ACTING:
                return True
            elif c in "sy":
                delimiter = script[i + 1]
                end = sed_past(script, i + 1, delimiter)
                end = sed_past(script, end - 1, delimiter)
                flags = ""
                while end < n and script[end] not in ";\n}":
                    flags += script[end]
                    end += 1
                if c == "s" and SED_ACTING & set(flags):
                    return True
                i = end
            elif c in SED_TO_LINE_END:
                i = script.find("\n", i)
                i = n if i < 0 else i
            else:
                i += 1
    except IndexError:
        return True
    return False


def sed_past(script, start, delimiter):
    """The index just past a delimited part whose opening delimiter is at
    `start`; IndexError when it never closes."""
    i = start + 1
    while True:
        c = script[i]
        if c == "\\":
            i += 2
            continue
        if c == delimiter:
            return i + 1
        i += 1


def prefix_words(grant):
    """A grant in a few words: "`cd` and `git push` commands"."""
    names = [f"`{p}`" for p in grant["prefixes"]]
    words = names[0] if len(names) == 1 else (
        ", ".join(names[:-1]) + f" and {names[-1]}"
    )
    return f"{words} commands"
