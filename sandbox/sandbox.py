"""The bwrap sandbox every command runs in.

Read-only system (the machine's /tmp included), one writable workspace, a
scratch folder per session ($TMPDIR, `Scratch`) at the same path in the box
and out, kept while the server runs, no network, the desktop's and the
machine's sockets covered, the user's secrets hidden: Codex's Linux shape.
What a later session trusts stays read-only in the workspace, missing or not:
its ZERO.md and git's folders (placeholders.py keeps a missing one's place).
readOnly's sandbox keeps the workspace read-only too, and so does every
sandbox whose workspace is /: binding / writable would open the whole
machine and cover the /dev and /proc mounts. The file tools' sandboxed_io runs
through the same command (files/). A call whose ask to leave the
sandbox was approved runs in Outside: bare, as the user (no root guard).
ZERO's home (its settings and keys, its history, its socket) and keys are
hidden from every box by construction, the evals' boxes included.
"""

import json
import os
import re
import shutil
import stat
import sys
import tempfile
import weakref
from pathlib import Path

import paths
from model.servers import ModelServerError, key_envs
from sandbox import placeholders

# Hidden behind an empty folder or file, in the user's home: keys and
# keyrings, cloud and package registries' logins, browsers' profiles (their
# cookies and saved passwords), other agents' logins. A command's output goes
# to the model's server, so what it can read can leave the machine
SECRETS = [
    ".ssh", ".gnupg", ".claude", ".codex", ".aws", ".azure", ".docker", ".kube",
    ".password-store", ".mozilla", ".thunderbird", ".config/gh", ".config/hub",
    ".config/gcloud", ".config/op", ".config/Claude", ".config/google-chrome",
    ".config/chromium", ".config/BraveSoftware", ".config/microsoft-edge",
    ".config/vivaldi", ".config/git/credentials", ".local/share/keyrings",
    ".local/share/kwalletd", ".netrc", ".git-credentials", ".npmrc", ".pypirc",
    ".vault-token", ".cargo/credentials.toml", ".gem/credentials",
    ".terraform.d/credentials.tfrc.json",
]
# The machine's sockets a command could reach through the read-only system:
# the docker daemon's (root on the machine), the system bus, X11's and the
# terminal multiplexers' (a shell outside any sandbox), container runtimes'
SOCKETS = [
    "/run/docker.sock", "/var/run/docker.sock", "/run/dbus", "/run/containerd",
    "/run/podman", "/run/snapd.socket", "/run/snapd-snap.socket",
    "/run/libvirt", "/run/screen", "/run/cups", "/run/avahi-daemon",
]
# The machine's /tmp, where sockets come and go (X11's, tmux's, an agent's, a
# browser's): each entry at its top that is a socket, or holds one this many
# folders down, is covered whole; the look stops after this many entries
TMP = Path("/tmp")
SOCKET_DEPTH = 3
SOCKET_LOOK_ENTRIES = 5000
# A path covered goes on bwrap's line when looked at, and may be gone when
# bwrap starts (a socket's folder in /tmp, removed meanwhile): bwrap can't
# make its mount point on the read-only system, or mount on it, and stops
# before the command runs. Its messages then (a path in the box under
# /newroot), and how many times such a command starts, a fresh look each time
VANISHED = re.compile(r"bwrap: Can't (?:mkdir|create file at|mount tmpfs on"
                      r"|bind mount \S+ on) (?:/newroot)?(/.+?): ")
START_TRIES = 3
# The environment's pointers to sockets and displays the box hides
POINTERS = (
    "DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK", "GPG_AGENT_INFO", "DISPLAY",
    "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "TMUX", "TMUX_PANE",
    "STY",
)
# Git's own folder: kept read-only at the workspace's top and in the
# repositories this many folders below it; the look stops after this many
# folders
GIT = ".git"
GIT_DEPTH = 2
GIT_LOOK_FOLDERS = 1000
# The stand-ins' runner (placeholders.py), run by path before ZERO loads
STAND_INS = Path(placeholders.__file__).resolve()
# The network as the line says it: none, each command in a network of its
# own, so a server a job runs is out of another command's reach
NO_NETWORK = (
    "there is no network, and each command has a loopback of its own, so a "
    "server one command starts can't be reached from another"
)
# How long the scratch folder lasts, as the line says it: the server keeps it
# while it runs (Scratch)
LASTS = (
    "the scratch folder is kept until ZERO restarts, and a new, empty one "
    "takes its place then"
)
# Facts a model read wrongly without: a `ps` that showed no job read as the
# job gone, a `cat` of SOUL.md that came back empty as the files empty
OWN_PROCESSES = (
    "each command sees only its own processes, so ps there shows neither a "
    "job nor another command (the job tool shows the jobs)"
)
HIDDEN_HOME = (
    "ZERO's own folder ({home}), where SOUL.md and the user's ZERO.md are, "
    "is hidden, so its files read as missing there; this system message "
    "holds what they held"
)
# Beside HIDDEN_HOME, where ZERO's own skills folder is shown again
SKILLS_SHOWN = (
    "; its skills folder ({skills}) is shown again, read-only, so a skill "
    "there is read and its scripts run as anywhere else"
)
# What the sandbox's walls look like in a command's output
WALL_MARKS = (
    "Read-only file system",
    "Network is unreachable",
    "Could not resolve host",
    "Temporary failure in name resolution",
)
# The argument a tool takes to run one call outside the sandbox, declared
# here and offered by the shell and the file tools under this name
OUTSIDE_ARGUMENT = "outside_sandbox"
# What a call's result gets when it met a wall, and when it asked to leave
# where nothing can (the shell's and the file tools')
WALL = "(This ran in the sandbox: {walls}.)"
NOT_OUTSIDE = (
    f"({OUTSIDE_ARGUMENT} isn't available in this session, so it ran in the "
    "sandbox.)"
)
# What a call that asks to leave crosses, in the one sentence the user, the
# consent check and the judge all read
OUTSIDE = "It runs outside the sandbox, with the user's permissions and the network."


def outside_crossing(args):
    """What a call that asks to run outside the sandbox crosses (the
    `outside_sandbox` the shell and the file tools offer), or None: the
    tools' `crossing` (core/tools.py)."""
    if args.get(OUTSIDE_ARGUMENT) is not True:
        return None
    return {"rule": OUTSIDE_ARGUMENT, "fact": OUTSIDE, "why": args.get("reason")}


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
                 protected=(), scratch=None, shown=(), own_skills=None):
        self.workspace = Path(workspace).resolve()
        # The harness owner's own folders (ZERO's home with its keys and
        # history; private_folders): hidden from every command
        self.private = [Path(p) for p in private]
        # Folders inside a hidden one shown again, read-only, at their own
        # path: the session's results folder (a callable each, as a new
        # session's log is named late)
        self.shown = list(shown)
        # ZERO's own skills folder, among those shown, which the environment
        # line names: its skills' scripts run in here (None: none shown)
        self.own_skills = own_skills
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
        # Covered before the workspace, the scratch folder and the folders
        # shown again are bound, so each comes back on top of a hidden one
        for path in self.covered_folders():
            if path.exists():
                args += ["--tmpfs", str(path)]
        args += self.hiding()
        # At its own path, so a path means one place in the box and out
        args += ["--bind", tmp, tmp]
        stand_ins = []
        if self.read_only:
            # Already read-only under / ; bound again only to come back from
            # under a folder kept out of view. Never / itself, which would cover
            # the /dev, /proc and scratch mounts above
            if self.workspace != Path("/"):
                args += ["--ro-bind", ws, ws]
        else:
            args += ["--bind", ws, ws]
            for path, kind in self.kept():
                if not os.path.lexists(path) or placeholders.is_marked(path):
                    stand_ins.append((str(path), kind))
                args += ["--ro-bind", str(path), str(path)]
        args += self.shown_again()
        args += ISOLATION + ["--chdir", ws, "--"]
        if stand_ins:
            args = [sys.executable, "-I", str(STAND_INS), json.dumps(stand_ins),
                    "--", *args]
        return args + list(argv)

    def start_again(self, tries, exit_code, output_head):
        """Whether a command started `tries` times goes again: bwrap stopped
        before it ran, a path to cover gone since the look."""
        return tries < START_TRIES and cover_vanished(exit_code, output_head)

    def kept(self):
        """What stays read-only in a writable workspace, as (path, "file" or
        "folder"): the files a later session reads as instructions (ZERO.md)
        and git's own folder, at the top whether or not they exist (a
        stand-in keeps a missing one's place), and the repositories' found
        below it."""
        kept = [(self.workspace / name, "file") for name in self.protected]
        kept.append((self.workspace / GIT, "folder"))
        return kept + [(git, "folder") for git in inner_gits(self.workspace)]

    def covered_folders(self):
        """Folders covered with empty ones before the workspace is mounted,
        so a workspace inside one stays visible: none here; the evals' exam
        room names its own (dev/evalkit/exam.py)."""
        return []

    def covers(self):
        """What the box hides, as (path, "file" or "folder"): the login
        session's runtime folder (the D-Bus session bus, ssh-agent, gpg-agent
        and Wayland sockets), the user's secrets, the machine's sockets, and
        the harness owner's own folders; only what exists."""
        runtime = Path(f"/run/user/{os.getuid()}")
        hidden = [runtime] + [self.home / name for name in SECRETS]
        hidden += [Path(path) for path in SOCKETS] + tmp_socket_holders()
        hidden += self.private
        covers = []
        for path in hidden:
            if path.is_symlink() and Path(os.path.realpath(path)) in hidden:
                continue
            if path.is_dir():
                covers.append((path, "folder"))
            elif os.path.lexists(path):
                covers.append((path, "file"))
        return covers

    def hiding(self):
        """bwrap's arguments that cover each hidden path with an empty folder,
        or a file with an empty one."""
        args = []
        for path, kind in self.covers():
            if kind == "folder":
                args += ["--tmpfs", str(path)]
            else:
                args += ["--ro-bind", "/dev/null", str(path)]
        return args

    def shown_again(self):
        """The folders inside a hidden one shown again, read-only, once they
        exist."""
        args = []
        for folder in self.shown:
            path = str(folder() if callable(folder) else folder)
            args += ["--ro-bind-try", path, path]
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
        for name in POINTERS:
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
        hidden = []
        if self.private:
            shown = (SKILLS_SHOWN.format(skills=self.own_skills)
                     if self.own_skills is not None else "")
            hidden = [HIDDEN_HOME.format(home=self.private[0]) + shown]
        seen = "; ".join([OWN_PROCESSES, *hidden])
        if self.read_only:
            return (
                "sandbox: on, read-only. The system is read-only, /tmp included; "
                f"only this session's scratch folder, $TMPDIR ({tmp}), is "
                f"writable; {LASTS}; {seen}; {NO_NETWORK}"
            )
        # .git stays read-only (its hooks and config run outside, later), so
        # the line says how a repository changes, not a list of what's locked
        return (
            "sandbox: on. The system is read-only, /tmp included; only the "
            f"workspace and this session's scratch folder, $TMPDIR ({tmp}), are "
            f"writable; {LASTS}; git's own folder (.git) is read-only, and one "
            "can't be made, so a git command that changes or makes a repository, "
            f"such as a commit or git init, runs outside the sandbox; {seen}; "
            f"{NO_NETWORK}"
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

    def start_again(self, tries, exit_code, output_head):
        """A bare command starts once."""
        return False

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


def mark_placeholder(path):
    """Mark a path as a stand-in a command left (the tests' killed command)."""
    placeholders.mark(path)


def cover_vanished(exit_code, output_head):
    """Whether bwrap stopped before the command ran because a path it was to
    cover went away after the look: nothing ran, so it may start again."""
    if exit_code != 1:
        return False
    match = VANISHED.match(output_head)
    return match is not None and not os.path.lexists(match.group(1))


def tmp_socket_holders():
    """The entries at /tmp's top that are sockets or hold one within
    SOCKET_DEPTH folders, looked for a level at a time across all of them,
    so a socket near the top (X11's, tmux's, an ssh agent's) is found however
    full the deep folders are: the top's own folders are each looked at up
    to SOCKET_LOOK_ENTRIES entries, the levels below up to that many in
    all."""
    try:
        entries = list(os.scandir(TMP))
    except OSError:
        return []
    tops, found, level = [], set(), []
    for entry in entries:
        try:
            mode = entry.stat(follow_symlinks=False).st_mode
        except OSError:
            continue
        path = Path(entry.path)
        if stat.S_ISSOCK(mode):
            tops.append(path)
            found.add(path)
        elif stat.S_ISDIR(mode):
            tops.append(path)
            level.append((path, entry.path))
    looked = 0
    for depth in range(1, SOCKET_DEPTH + 1):
        deeper = []
        for top, folder in level:
            if top in found:
                continue
            seen = 0
            try:
                listed = os.scandir(folder)
            except OSError:
                continue
            with listed:
                for entry in listed:
                    seen += 1
                    if seen > SOCKET_LOOK_ENTRIES:
                        break
                    try:
                        mode = entry.stat(follow_symlinks=False).st_mode
                    except OSError:
                        continue
                    if stat.S_ISSOCK(mode):
                        found.add(top)
                        break
                    if stat.S_ISDIR(mode) and depth < SOCKET_DEPTH:
                        deeper.append((top, entry.path))
            if depth > 1:
                looked += seen
                if looked >= SOCKET_LOOK_ENTRIES:
                    break
        if looked >= SOCKET_LOOK_ENTRIES:
            break
        level = deeper
    return [top for top in tops if top in found]


def inner_gits(workspace):
    """The git folders (or a worktree's .git file) of repositories a few
    folders below the workspace's top."""
    found, looked = [], 0
    folders = [(workspace, 0)]
    while folders and looked < GIT_LOOK_FOLDERS:
        folder, depth = folders.pop()
        looked += 1
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False) or entry.name == GIT:
                continue
            inner = Path(entry.path)
            if os.path.lexists(inner / GIT) and not placeholders.is_marked(
                    inner / GIT):
                found.append(inner / GIT)
            if depth + 1 < GIT_DEPTH:
                folders.append((inner, depth + 1))
    return found


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
