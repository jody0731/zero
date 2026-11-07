"""The bwrap sandbox every command runs in.

Read-only system (the machine's /tmp included), one writable workspace, a
scratch folder per session ($TMPDIR, `Scratch`) at the same path in the box
and out, kept while the server runs, no network, no desktop sockets, secrets
hidden: Codex's Linux shape.
readOnly's sandbox keeps the workspace read-only too, and so does every
sandbox whose workspace is /: binding / writable would open the whole
machine and cover the /dev and /proc mounts. The file tools' sandboxed_io runs
through the same command (files/). A call whose ask to leave the
sandbox was approved runs in Outside: bare, as the user (no root guard).
ZERO's home (its settings and keys, its history, its socket) and keys are
hidden from every box by construction, the evals' boxes included.
"""

import os
import shutil
import tempfile
import weakref
from pathlib import Path

import paths
from model.servers import ModelServerError, key_envs

# Hidden behind an empty folder: keys, the gpg keyring, Claude's own login
SECRETS = [".ssh", ".gnupg", ".claude"]
# How long the scratch folder lasts, as the line says it: the server keeps it
# while it runs (Scratch)
LASTS = (
    "the scratch folder is kept until ZERO restarts, and a new, empty one "
    "takes its place then"
)
# What the sandbox's walls look like in a command's output
WALL_MARKS = (
    "Read-only file system",
    "Network is unreachable",
    "Could not resolve host",
    "Temporary failure in name resolution",
)
# What a call's result gets when it met a wall, and when it asked to leave
# where nothing can (the shell's and the file tools')
WALL = "(This ran in the sandbox: {walls}.)"
NOT_OUTSIDE = (
    "(outside_sandbox isn't available in this session, so it ran in the sandbox.)"
)
# What a call that asks to leave crosses, in the one sentence the user, the
# consent check and the judge all read
OUTSIDE = "It runs outside the sandbox, with the user's permissions and the network."


def outside_crossing(args):
    """What a call that asks to run outside the sandbox crosses (the
    `outside_sandbox` the shell and the file tools offer), or None: the
    tools' `crossing` (core/tools.py)."""
    if args.get("outside_sandbox") is not True:
        return None
    return {"rule": "outside_sandbox", "fact": OUTSIDE, "why": args.get("reason")}


def private_folders():
    """What every sandbox hides of ZERO's own, read when a session is made:
    its whole home (the keys, the servers, other sessions' logs, the
    database, the socket), the socket's folder where a home too deep for a
    socket keeps it elsewhere, and the usual home when ZERO runs on another,
    whose files a second home may link to."""
    top = paths.home()
    run = paths.run_folder(top)
    hidden = [top] if top in run.parents else [top, run]
    usual = paths.usual_home()
    if usual.absolute() != top.absolute():
        hidden.append(usual)
    return hidden


# Its own user, process, IPC and host-name namespaces, no network, no
# capabilities; it ends with the harness
ISOLATION = [
    "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
    "--unshare-net", "--cap-drop", "ALL", "--new-session", "--die-with-parent",
]


class SandboxError(Exception):
    """The sandbox can't be set up, so the command must not run."""


class Scratch:
    """A session's scratch folder, its $TMPDIR: a real folder, made on first
    use, removed when the last one holding it lets it go. The server keeps
    each session's while it runs, so a session built again (a switch of mode,
    model or workspace) finds its files."""

    def __init__(self):
        self.made = None

    def path(self):
        if self.made is None:
            self.made = tempfile.mkdtemp(prefix="agent-tmp-")
            weakref.finalize(self, shutil.rmtree, self.made, True)
        return self.made


class Sandbox:
    sandboxed = True

    def __init__(self, workspace, home=None, read_only=False, private=(),
                 protected=(), scratch=None):
        self.workspace = Path(workspace).resolve()
        # The harness owner's own folders (ZERO's home with its keys and
        # history; private_folders): hidden from every command
        self.private = [Path(p) for p in private]
        # readOnly, or a workspace of /: mounted read-only like the rest
        self.read_only = read_only or self.workspace == Path("/")
        self.home = Path(home or Path.home())
        # Files in the workspace kept read-only, as its .git is: the ones a
        # later session reads as instructions (ZERO.md)
        self.protected = tuple(protected)
        # The session's scratch folder, or one of this box's own
        self.scratch = scratch or Scratch()
        self.tmp = None

    def command(self, argv):
        """Wrap argv in bwrap. Raises SandboxError rather than run it bare."""
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise SandboxError("bwrap is not installed, so nothing can run")
        self.workspace.mkdir(parents=True, exist_ok=True)
        tmp = self.private_tmp()
        ws = str(self.workspace)
        args = [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
        # Before the workspace bind, so the workspace comes back on top
        for path in self.covered_folders():
            if path.exists():
                args += ["--tmpfs", str(path)]
        # At its own path, so a path means one place in the box and out
        args += ["--bind", tmp, tmp]
        if self.read_only:
            # Already read-only under / ; bound again only to come back from
            # under a folder kept out of view. Never / itself, which would cover
            # the /dev, /proc and scratch mounts above
            if self.workspace != Path("/"):
                args += ["--ro-bind", ws, ws]
        else:
            args += ["--bind", ws, ws]
            git = self.workspace / ".git"
            if git.exists():
                args += ["--ro-bind", str(git), str(git)]
            for name in self.protected:
                kept = self.workspace / name
                if kept.is_file():
                    args += ["--ro-bind", str(kept), str(kept)]
        args += self.hiding()
        args += ISOLATION + ["--chdir", ws, "--"]
        return args + list(argv)

    def covered_folders(self):
        """Folders covered with empty ones before the workspace is mounted,
        so a workspace inside one stays visible: none here; the evals' exam
        room names its own (dev/evalkit/exam.py)."""
        return []

    def hiding(self):
        """bwrap's arguments that cover the hidden folders with empty ones:
        the D-Bus session bus, ssh-agent, gpg-agent and Wayland sockets, the
        keys, and the harness owner's own folders."""
        runtime = Path(f"/run/user/{os.getuid()}")
        hidden = [runtime] + [self.home / name for name in SECRETS]
        hidden += self.private
        args = []
        for path in hidden:
            if path.exists():
                args += ["--tmpfs", str(path)]
        return args

    def private_tmp(self):
        """This session's scratch folder, its $TMPDIR, at the same path in
        the sandbox and out."""
        if self.tmp is None:
            self.tmp = self.scratch.path()
        return self.tmp

    def env(self):
        """The command's environment, minus pointers to the hidden sockets and
        the harness's own keys."""
        env = harness_free_env()
        for name in ("DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK", "GPG_AGENT_INFO"):
            env.pop(name, None)
        env["TMPDIR"] = self.private_tmp()
        return env

    def walls(self):
        """What this sandbox stops, in the words the model reads."""
        if self.read_only:
            return "nothing is writable but $TMPDIR, and there is no network"
        return (
            "nothing outside the workspace and $TMPDIR is writable, and there is "
            "no network"
        )

    def environment_line(self):
        """What the session's environment says of this box (the prompt)."""
        tmp = self.private_tmp()
        if self.read_only:
            return (
                "sandbox: on, read-only. The system is read-only, /tmp included; "
                f"only this session's scratch folder, $TMPDIR ({tmp}), is "
                f"writable; {LASTS}; there is no network"
            )
        # .git stays read-only (its hooks and config run outside, later), so
        # the line says how a repository changes, not a list of what's locked
        return (
            "sandbox: on. The system is read-only, /tmp included; only the "
            f"workspace and this session's scratch folder, $TMPDIR ({tmp}), are "
            f"writable; {LASTS}; git's own folder (.git) is read-only, so a git "
            "command that changes a repository, such as a commit, runs outside "
            "the sandbox; there is no network"
        )


class Unsandboxed:
    """auto+ and bypassPermissions: the command runs as the user, bare, in the
    workspace."""

    sandboxed = False

    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()

    def command(self, argv):
        self.workspace.mkdir(parents=True, exist_ok=True)
        return list(argv)

    def env(self):
        return harness_free_env()

    def environment_line(self):
        """What the session's environment says of this box (the prompt)."""
        return "sandbox: off. Commands run as the user, with the network"


class Outside(Unsandboxed):
    """One call let out of the sandbox: bare, as the user, with what the user's
    account can do (no root guard). Its $TMPDIR is the session's scratch
    folder, at the path the sandbox's commands see it."""

    def __init__(self, workspace, sandbox=None):
        super().__init__(workspace)
        self.sandbox = sandbox

    def env(self):
        env = super().env()
        if self.sandbox is not None:
            env["TMPDIR"] = self.sandbox.private_tmp()
        return env


def harness_free_env():
    """This process's environment without the harness's API keys: whatever a
    command can print ends up with the model and in the trajectory. Every
    server's key variable, the user's servers' too; when models.toml can't
    be read, no command runs (a variable it names can't be known)."""
    try:
        names = key_envs()
    except ModelServerError as e:
        raise SandboxError(f"no command runs until this is fixed: {e}") from e
    env = dict(os.environ)
    for name in names:
        env.pop(name, None)
    return env
