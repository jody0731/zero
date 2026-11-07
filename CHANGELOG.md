# Changelog

What each release of ZERO brought, newest first. A release is one checkpoint of ZERO's development, or a fix
between two (a fourth number); versions below 1.0 may change anything.

## 0.6.2.8.1

A fix release: the names 0.6.2.8 missed.

- **The state database's times end in `_at`:** the first start after the update renames
  `state/state.sqlite`'s columns in place. A copy of the database as it was is kept beside it first
  (`state.sqlite.before-renames-<time>`). Going back to 0.6.2.8 needs that copy.
  - Sessions: `created_at`, `updated_at`. The inbox: `created_at`, `resume_count`. Asks: `created_at`,
    `closed_at`.
  - Jobs: `exit_code`, `started_at`, `ended_at`, `due_at`, `next_checkin_at`, `checkin_count`, `read_bytes`.
- The rows `session/list` and `job/list` send carry the same names, so the protocol is 4. A client of 4
  restarts an idle server of 3, as before, and names a busy one.
- Texts that still said the old names now say the new ones; one failure message the model reads no longer
  names ZERO's machinery ("the file operation in the sandbox failed").

## 0.6.2.8

Every name says what it is. Your files keep working: old names still read, and nothing is rewritten until you
save a setting.

- **ZERO's host is now its server:** `zero server status|stop|hello|run` (was `zero host …`). The systemd user
  unit is `zero-server-<hash>`, and its log `state/server.log`. The first command after the update restarts
  the old host once ("ZERO's server runs older code: restarting it."). A busy one is left running and named.
- **"Provider" is now "model server":** `zero model server list|connect|disconnect|probe` (was
  `zero model provider …`). In `models.toml` a server's display name is `title` and its window
  `context_window` (old `name` and `context` still read).
- **Settings renamed** (old names still read; a new name wins when both are set): `idle_minutes` →
  `server_idle_minutes`, `effort` → `thinking_effort`, `judge_effort` → `judge_thinking_effort`,
  `output_limit` → `bash_output_chars`, `web_chars` → `web_fetch_chars`, `web_results` → `web_search_results`,
  `retry_base` → `model_retry_seconds`, `step_grant` → `extra_steps`, `bash_timeout` →
  `bash_foreground_seconds`, `model_timeout`, `judge_timeout`, `consent_timeout` → `*_silence_seconds` (each
  counts only silence), `max_format_errors` → `max_broken_replies`.
- **The full screen's table is `[tui]`** (was `[cli]`, still read; the next save writes `[tui]`). Key ids
  `elsewhere`, `folds`, `next_needing`, `go_back` → `new_elsewhere`, `toggle_folds`, `next_needing_you`,
  `previous_session`.
- **Commands:** `--thinking-effort`, `--judge-thinking-effort`, `--context-window` (`--effort`, `--judge-effort`,
  `--context` still work); `-r` is also `--resume`, `-p` also `--print`.
- **In `/settings`, "Dev…" is "Advanced…",** and its "Host" group "Server".
- **The job tool's `kill` is `stop`.**
- The protocol is 3: `server/hello`, `server/stop`, `model/server/*`, `session/mark_seen`, and fields renamed
  where they misled (`not_ready_reason`, `key_state`, `busy_with`, `started_at`, `measured_at`,
  `context_window`, `thinking_effort`, …). A client of 3 still talks to a server of 2 until it restarts it.

## 0.6.2.7

ZERO speaks each server's own API, and measures what a model honours.

- **The Responses API:** a server's entry names the API it speaks, `api = "responses"` or `"chat_completions"` (the default) in `models.toml`. DeepSeek now goes through the Responses API, where it takes strict JSON; local servers (Ollama, llama-server) stay on the Chat Completions API.
- **Thinking off reaches every server:** "off" is sent as each API's own field (`reasoning_effort: "none"`, `reasoning.effort: "none"`), so llama-server and Ollama turn thinking off too; before, only DeepSeek got it.
- **What a model honours, measured:** thinking off, thinking, strict JSON and JSON mode, each by a small request, the first time a model's settings are shown (a few calls), kept per model. `zero model features MODEL` says what was measured; `--test` measures again. A value a model doesn't honour shows **unavailable on MODEL** on its row in `/settings`, following the model it runs on, and is kept but not sent.
- **The judge and the consent check** ask for strict JSON wherever the model honours it, else JSON mode.
- **Replies are read by each API's rules:** a reply that didn't complete (a finish reason the API doesn't name, a failed or incomplete response) is retried, whatever the server.
- **models.toml:** the lines `thinking` and `json_format` are gone (measured now); a file that has them says so.
- The protocol (still 2): `model/features`; `setting/list` rows say which feature each value needs (`needs`) and which setting names its model (`runs_on`).

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
