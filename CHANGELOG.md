# Changelog

What each release of ZERO brought, newest first. A release is one checkpoint of ZERO's development, or a fix
between two (a fourth number); versions below 1.0 may change anything.

## 0.6.4

Long work in rounds.

- **Work in rounds:** leave ZERO a long job ("I'll check back"), or send it with `--rounds` (`zero session
  send` and `zero -p`), and ZERO starts work in rounds with its tool, with an estimate (rounds, minutes,
  dollars) and a done check, a command such as `pytest -q`. Each round is a turn of the same session whose
  context starts fresh from the last round's handoff (every handoff kept in the session's folder); the
  server starts the next one until the work is done (the done check passes, run by the server), stuck
  (ZERO says why, or two rounds in a row failed), stopped, or its budget is spent, when ZERO reports and
  asks for a new one. A turn whose context was compacted twice is told it can go on in rounds.
  - Nothing in a round waits on you: you approve the budget at the start and read the report at the
    end. In between, a call that would ask you is refused with its reason (in auto and auto+ the judge
    answers as before), and a round is bounded by the run's budget, not the turn's.
  - `zero rounds list` and `zero rounds stop ID`; the status line shows `round 3/8 · 42 min of 2 h · $0.12
    of $1.00`. Work in rounds survives a restart of the server.
- **A budget for every turn:** **Minutes a turn runs before asking** (`standing_budget_minutes`, 120) and
  **Dollars a turn spends before asking** (`standing_budget_dollars`, 2.00), under Limits. Minutes are
  ZERO's own working time; dollars are its model calls at the model's price (nothing on a local server).
  Past either, ZERO asks you to let the turn go on, in every mode, never the judge; a yes gives it as much
  again. An estimate for work in rounds past it asks you too.
  - It replaces the step limit: `max_steps` and `extra_steps` are retired (ignored in config.toml), and
    `max_seconds` reads as the minutes. A turn's twenty refusals by the judge no longer end it; three in a
    row still do.
- **The judge's ceiling:** **Tokens a verdict may write** (`judge_reply_tokens`, 8,192, thinking included).
  A verdict cut there is asked again once, then counts as a deny.
- **A fallback model** (`fallback_model`, unset): when a call to the session's model fails past its
  retries, the rest of the turn goes on with it, and the next turn tries the session's model again. A judge
  whose model is down asks it too.
- The protocol stays 4: `session/send` takes `rounds`, and `rounds/list` and `rounds/stop` are new.
- **Fixed:** after an update, a session's next turn no longer counts its context several times high
  and compacts it for nothing.
- **Fixed:** the job tool finds a job by its id without the `job-` prefix.

## 0.6.3

Long sessions keep going.

- **Compaction in place of the hard stop:** before a call that wouldn't fit under its line, a session's
  older outputs are saved to files and cleared from its context; when that isn't enough, ZERO writes
  itself a handoff (your requests, what is done, the state, what is next) and goes on from it with its
  newest steps. The log keeps everything.
  - The line is the new setting **Compact at (tokens)** (`compact_at_tokens`, under Advanced…); unset, the
    model's context window. `context_stop` reads as it; the catalog's 100,000-token stops are gone.
  - `/compact`, `zero session compact` and `session/compact` compact now (before the next step while a
    turn runs); ZERO has a `compact` tool too.
- **No fixed caps on what ZERO reads:** a result past its share of the room left (15%) is saved whole in
  `state/sessions/results/` (read-only in the sandbox), and ZERO reads its start and end, its size and
  where it is. `read` takes a whole file or the lines asked; `web_search`'s count is ZERO's to choose.
  `bash_output_chars`, `read_chars`, `web_fetch_chars`, `web_search_results` and `judge_outputs` are
  retired (ignored in config.toml). SOUL.md and the ZERO.md files share 15% of the line.
- **The meter:** the status line and `zero session read` show `context 42k/1M · cache 96%`, the context's
  size against its line and the share of the last call the server's cache served. A compaction shows in
  the transcript.
- **Times:** each message ZERO reads from you ends with when you sent it.
- **The judge** reads a proposed write, edit or plan whole, keeps every command's output in its view (so
  the server's cache serves almost all of it), and knows your messages together set the goal.
- **readOnly:** ZERO no longer says a write in readOnly would ask you; every tool says a call goes to the
  session's approver, whom the mode names.
- **Safe at the edge:** ZERO counts what it adds with the model server's own tokenizer where the server
  offers one (llama.cpp), and otherwise as a token a byte, which never counts too few; digits, hashes and
  JSON could fool the old estimate past a server's limit.
- **No more give-ups while a local server reads:** where a model honours it, ZERO asks llama.cpp for
  its progress through a long prompt and waits while it reads; every model call's silence limit is now
  300 s (it was 45 s for the model). A `model_silence_seconds` you set stays yours.
- The system message is written again only at a compaction, a model switch or a new release. The
  protocol stays 4.

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
