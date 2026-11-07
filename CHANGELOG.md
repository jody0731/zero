# Changelog

What each release of ZERO brought, newest first. A release is one checkpoint of ZERO's development, or a fix
between two (a fourth number); versions below 1.0 may change anything.

## 0.6.2.6.1

A fix release.

- **No more "Connect a model" while models are connected:** with no default model set but a server connected, `zero` opens **New sessions' model**, a pick of the models your sessions used and the servers serve (another typed, or **Connect a model…** last); the pick is the default. "Connect a model" opens when nothing is connected or the default's key is missing; before, every start with no default landed there.
- **New sessions' model can't be unset by a key:** `/settings` → Default → Model has no "none set" row, and Del leaves it as it is. Removing its server still unsets it, and says so. The judge's model keeps its unset choice, the session's own model.

## 0.6.2.6

The settings in sections, `/model` with the thinking, and a judge that decides last.

- **The settings view in sections:** Current session (its mode, model and thinking effort, from its next turn), Default (what new sessions start with; was New sessions), TUI (the theme and the mouse; was Look), Models and keys…, Key bindings…, and **Dev…**, a view of its own: the judge (for this session and by default), the limits and the host.
- **The lists scroll with the selection,** in the settings views and the key bindings; before, ↑ and ↓ could move it off screen.
- **`/model`** opens the session's model and its thinking effort; `/model NAME` still switches the model alone. **`/effort` is gone** (it's in `/model`), and so is **`/history`**: scrolling to the top brings earlier records.
- **The judge decides last:** it writes its reason, then a second look at the reason (where it could be wrong, checked against the conversation), then its decision. Each verdict keeps its second look.
- **The judge's thinking is off by default.** A session made before keeps the judge it was made with.
- **A session's judge can change mid-session,** from its next turn: Dev's rows for this session, or `zero session set ID --judge-model M --judge-effort E` (`--judge-model ""`: the session's own model). The change is logged, and shows in the transcript, but isn't a note the model reads.
- **A judge with no model of its own follows the session's model**, after `/model` too; before, it stayed on the model the session was made with.
- **A thinking effort chosen before a session's first message now counts:** it goes with that message (`zero -p TEXT --effort off`, `zero session send --effort`).
- The protocol (still 2): `session/set` takes `judge_model` and `judge_effort`, `session/send` a new session's `effort`, and `session/read`'s reply says what the session's next turn runs with (`runs_with`; `limit` 0 reads no records). `zero session read` shows it.

## 0.6.2.4.3

A fix release.

- **The scratch folder lasts while ZERO runs:** a session keeps its `$TMPDIR`, and the files in it, when its mode, model or workspace changes; before, the change emptied it. It goes when ZERO's host stops, and the session's environment line now says so.

## 0.6.2.4.2

A fix release.

- **The TUI's input boxes are round,** as its other boxes are. Their old edge drew its left side as a reversed block, and in a terminal whose block falls short of the cell a second, faint line showed beside it.
- The README's screenshots draw the TUI's block characters as shapes, without gaps between the rows.

## 0.6.2.4.1

One path, one place: a path means the same place in the sandbox and out. And a new README.

- **`/tmp` is the machine's:** in the sandbox it is read-only, as the rest of the system is, where before a folder of the session's own stood in its place. A command that writes there now fails with "Read-only file system"; before, the file landed in that folder and went with the session.
- **The session's scratch folder, `$TMPDIR`,** is writable and at the same path in the sandbox and out (`/tmp/agent-tmp-…`), so the sandboxed commands, the file tools and a command let out of the sandbox all mean one place by one path.
- A long web page is saved in the scratch folder, and the result gives its real path.
- **A new README:** ZERO's logo (its name on a dot-matrix panel, a pulse running through the E), a screenshot of the TUI, sections with the longer parts folded away, and a table for the MCP tools.

## 0.6.2.4

ZERO as a worker: one door for every app, and plain commands for all of it.

- **One protocol:** every app reaches the host through the same messages, now named noun/verb (`session/send`,
  `job/stop`, `model/provider/connect`) and published as JSON Schema in `protocol/schema.json`.
- **Plain commands** for everything a caller does, named as its messages: `zero session …`, `zero job …`,
  `zero grant …`, `zero model …`, `zero setting …`, `zero host hello|stop`. `--json` prints one JSON object; a key
  comes only from stdin (`--key-stdin`).
- `zero -p` (short for `zero session send`) takes `--cd`, `--when-busy`, `--choice` and `--no-wait`; `--json` now
  prints one result object (session, outcome, answer, waiting question, tokens, seconds) and `--events` every event.
- **`zero mcp serve`:** ZERO as an MCP server for other agents (`claude mcp add zero -- zero mcp serve`), with
  `zero_start`, `zero_wait`, `zero_reply` and `zero_stop`. A new requirement, `mcp`.
- A host still on the old protocol is replaced once it is idle, as a host on older code is.
- The full screen is an app of its own (`apps/tui/`); `zero` starts it, `zero mcp serve` and the host each from
  their own entry, so no app loads another.

## 0.6.2.3

Connecting a model, from ZERO itself.

- **Connect a model:** the first `zero` with no model opens a view for one: a provider from the catalog and its API
  key, or a server of your own (its URL, a key if it takes one, a model it serves, its context window where the
  server doesn't say). A one-token test call proves it before anything is saved; the first model connected becomes
  your default.
- **Models and keys** in `/settings`: connect another model, a new key, a test, remove a server.
- `zero -p` with no model set says how to connect one.
- With `ZERO_HOME` naming another home, the sandbox now hides `~/.zero` as well, so the commands ZERO runs never
  see the usual home's keys or sessions.

## 0.6.2.2.1

A fix release.

- Each home runs its host as a systemd unit of its own: `zero-host` for `~/.zero`, `zero-host-<hash>` for one that
  `ZERO_HOME` names elsewhere, so the hosts of two homes run side by side.
- `zero --help` names Z.E.R.O. as the README does.

## 0.6.2.2

The first release of Z.E.R.O., a general purpose agent.

Install it from this repository:

    git clone git@github.com:jody0731/zero.git ~/Applications/zero
    ~/Applications/zero/install.sh

From here on, `zero update` brings each new release, and its notes, to your terminal.

What's in it:

- **Model compatibility:** any server that speaks the OpenAI chat API, from a catalog ZERO ships or your own; no
  model is built in
- **Session management:** many sessions, each with its own workspace and history
- **Built-in permissions:** five modes, a bubblewrap sandbox, and a judge for the auto modes
- **Unified hosting:** one host runs every session; work goes on after the terminal closes
- **What makes it powerful:** it trusts the model; what it reads states facts, and the limits live in code

ZERO is below 1.0: anything may change between releases.
