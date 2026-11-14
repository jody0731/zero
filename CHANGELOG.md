# Changelog

What each release of ZERO brought, newest first. A release is one checkpoint of ZERO's development, or a fix
between two (a fourth number); versions below 1.0 may change anything.

## 0.7.6.2.1

- **Another agent finds its way in.** `zero --help` ends with how another agent connects:
  ZERO's MCP address on this machine and the line for Claude Code and for Codex. Tell an
  agent "use ZERO" and it can read them there and add ZERO itself.
- Where no user systemd answers, the install's line says `zero web` starts the door and
  agents use `zero mcp serve`.

## 0.7.6.2

ZERO in the browser, and one door for every agent.

- **The web UI.** `zero web` opens ZERO in your browser, signed in: your sessions with
  their subagents and forks, each turn as it happens (tools, asks, notes, drafts), an
  ask answered in place of the input, a session's mode, model, folder, context and
  running work beside it, a new session in any folder. It works on a phone-sized window
  too. An open page doesn't keep ZERO's server running; the page shows when it sleeps
  and wakes it.
- **ZERO's HTTP door** on `127.0.0.1:4747` serves the web UI and ZERO's MCP server at
  `/mcp`. systemd holds the port from login and starts the door at the first knock; the
  door leaves after five idle minutes, and ZERO's server still leaves when idle. Only
  your own programs get in: the door knows a caller by its connection as the socket
  does, a browser by the sign-in `zero web` gives, and it takes no other host or site.
  `zero install` and `zero update` put its two units in `~/.config/systemd/user`;
  `zero http-door status|install|uninstall` shows or changes them.
- **Claude Code and Codex by URL.** Point them at
  `http://127.0.0.1:4747/mcp` (`zero http-door status` prints the lines) and their
  sessions use ZERO with no `zero mcp serve` process of their own. `zero mcp serve`
  stays for stdio, lighter: no MCP library, a quarter of the memory (22 MB, was 88).
- **The MCP server speaks MCP's stateless 2026-07-28 revision** as well as the
  handshake of the ones before.

## 0.7.6.1

Apps on one layer: ZERO's server says what its apps used to know, and any program can be one.

- **Apps in any language.** `zero server start --json` names the server's socket, the token
  file and the protocol; `protocol/schema.json` describes every message, its reply and the
  rows replies carry. A program on this machine is known by the socket; where it isn't, the
  token in `~/.zero/app-token` (made by the server, mode 600) lets it in. See the README's
  "Apps in any language".
- **Sent twice, run once.** Every message that changes something carries an id, and the
  same id again runs nothing: a command retried after a dropped connection no longer starts
  a second session or a second fork. Its first reply comes back, marked `replayed`.
- **The server says what the apps knew by heart:** the modes, each with its line (Shift+Tab
  steps through the server's list, `/mode` shows each mode's line), a question's choices
  with their words, a model's server. `zero mcp serve`'s `zero_start` reply names the mode
  and its line; `zero --choice` takes any choice the question lists.
- **A tool's output kept out of the context names its file** as data, for an app to link.
- **`zero mcp serve` inside a workflow** hands `zero_start`'s task to a subagent of the
  session running it, as `zero -p` does there.
- **Older and newer side by side.** This server answers apps of the protocol before (4) in
  their own words, and an older `zero` no longer restarts a newer server into its own code.
  After updating, restart Claude Code (or any agent running `zero mcp serve`) so its helper
  runs the new release.

## 0.7.5.3

ZERO comes as one file with its own Python inside.

- **One file to install.** Each release is an archive for each machine: ZERO's code, the
  packages it was tested with and its own free-threaded Python (CPython 3.14.10 from
  python-build-standalone). Nothing else is installed: no Python, git or pip.
  `curl -fsSL https://github.com/jody0731/zero/releases/latest/download/install.sh | sh`
  installs the newest into `~/.local/share/zero` and the `zero` command into `~/.local/bin`; or
  download the archive, unpack it and run its `bin/zero install`.
- **`zero update` brings everything**, the code, its Python and its packages, and switches
  whole or not at all. The new release is fetched, its sha256 checked and tried (its version,
  and that its Python loads ZERO's server with the GIL off) before one rename makes it current;
  until then the release you had runs, and a failed update leaves it running. The one before
  stays until the next update, unless something still runs from it.
- **From a git clone (0.7.5.2 and older), nothing by hand.** `zero update` checks out this
  release as before; its next start moves ZERO to its own Python. It fetches this release's
  archive (about 65 MB), installs it in `~/.local/share/zero`, points `~/.local/bin/zero` at
  it and runs your command again. The server restarts on the new Python once it is idle. The
  clone (`~/Applications/zero`) is no longer used, and the next update removes it if it holds
  nothing of yours. A move that can't finish leaves the clone running as before, and is tried
  again an hour later, or at once with `zero update`.
- **`zero uninstall`** removes the releases with the command; `~/.zero` stays.
- **`zero doctor`** says whose Python ZERO runs on.
- **ZERO's own Python processes ignore the machine's Python settings** (`PYTHONPATH`,
  `PYTHONHOME`, the user's site-packages). The commands ZERO runs for you still see them.
- **A killed server's scratch folders** (`/tmp/agent-tmp-…`) are removed when the next server
  starts on the same home.

## 0.7.5.2

ZERO's sessions run side by side on every core.

- **Free-threaded Python.** On Python 3.14t (the free-threaded build) ZERO's sessions run truly
  in parallel; on Python with the GIL, ZERO works as before. `zero doctor` says which one ZERO
  runs on.
- **To move an install to free-threaded Python, once, by hand:** `zero update` brings this
  release on the Python the install already has, and nothing else is needed for it to work.
  To run on 3.14t: stop ZERO's server (`zero server stop`), then run `install.sh` in ZERO's
  folder (where it was cloned); it finds `python3.14t` on the PATH and makes ZERO's environment
  again with it. No `python3.14t`? `uv python install 3.14t` puts one in `~/.local/bin` with no
  sudo; or set `ZERO_PYTHON` to the Python you want before running `install.sh`.
- **Many sessions at once.** Each session keeps its own state behind its own lock, and the
  database has one writer that commits many writes together while reads never wait. On the
  stress test at 200 sessions a message is acked in under 0.2 s (5.6 s before) and ZERO
  finishes about four times as many turns a second.
- **Session ids** carry 8 random hex digits: two sessions made in the same second no longer
  collide.
- **A session found without its row is listed again.** A session's log carries what its row
  says; a log the database lost (a crash between the two) is listed again at the next start.
- **A deleted session lets go** of everything the server kept of it: its open watches end
  (`bye` "deleted"), its usage reader and its scratch folder go.
- **Session search over thousands of sessions** stays quick: a search ranks its best 5,000
  matches and lists the sessions past them after.

## 0.7.4.4

ZERO writes skills of its own, and a script it writes can call ZERO.

- **Skills ZERO writes.** `write_skill` keeps a skill (its `SKILL.md`, by the Agent Skills
  standard, and the scripts and files beside it) in `~/.zero/skills`, whole or not at all; every
  session that starts after lists it, and the session that wrote it reads it at once. Each write
  asks first, in every mode, as a change to ZERO's own files does. A name another skills folder
  uses is left to that folder.
- **Workflows.** `zero -p TEXT` run by one of a session's commands starts a subagent of that
  session and prints its answer: a script can run many at once. Each command carries its
  session's id in `ZERO_SESSION_ID`, and ZERO checks the command is that session's before it
  starts anything. The subagent runs in its session's workspace, mode and model; its asks go
  where that session's other subagents' go, and its answers to the script.
- **The sandbox's line** says ZERO's own skills folder is shown inside it, read-only, so a skill's
  scripts there run in the sandbox.

## 0.7.4.3

ZERO keeps working when its MCP servers bring more tools than fit.

- **Many tools, found by a search.** A server's tools are sent with each request while they fit
  their share of the context (5%, servers in your list's order); a server that doesn't fit is
  listed by name instead, and ZERO finds its tools with `tool_search` and calls one with
  `call_tool`. The call is the tool's own: it asks, and "this session" covers it, as if the tool
  had been sent. The tools sent never change on a search, so the model's cache keeps working.
  Put the servers you use most first in `~/.zero/mcp_servers.toml`, or narrow one with `--tools`.
- **ZERO's own tools are always sent.**

## 0.7.4.2

ZERO uses the tools of MCP servers, and runs a step's reads side by side.

- **MCP servers.** `zero mcp add NAME -- COMMAND` (or `--url URL`, or `--from claude` / `--from
  codex` to copy an entry you have) lists a server in `~/.zero/mcp_servers.toml`; `zero mcp list`,
  `get` and `remove` look after it. ZERO starts the servers in the background with its server,
  and a session is offered their tools as `mcp__NAME__TOOL`, their descriptions and answers marked
  as the server's. Each call runs outside the sandbox and asks as a command that leaves it does.
  A server that goes away is started again at the next call; a call is never sent twice.
- **A step's reads side by side.** Reads, skills read, web searches and fetches from the internet
  that the model asks for together run at the same time, and come back in order. Anything that
  could ask still runs alone.
- **`zero doctor`** names an MCP server whose command isn't found on the machine.

## 0.7.4.1

ZERO uses skills: folders of instructions, often with scripts, by the Agent Skills standard that
other agents share.

- **Skills it finds on its own.** ZERO reads skills from `~/.agents/skills` (shared by the agents
  on your machine), `~/.zero/skills` (its own) and a project's `.agents/skills`, nearest first. Its
  system message lists each with its author's description; when a task calls for one, ZERO reads
  it in full with `read_skill`, marked as the skill's own words, and runs its scripts as any
  command, under the session's mode.
- **`zero doctor` names skills it can't use** as they are: one with no description, frontmatter
  that isn't YAML, or a name found twice.
- **New requirement:** PyYAML, which `zero update` installs.

## 0.7.3.2

ZERO sizes its context closer to the truth on DeepSeek and OpenAI.

- **Its estimate learns from the model server's own counts.** Where the server refuses a request
  that is too long (DeepSeek, OpenAI), ZERO now estimates new text at the rate the server's counts
  show for this session, not at one token a byte, which read English about four times high. So
  results get fairer shares, compactions come when they should, and clearing old outputs is
  often enough. If a guess runs low there, the server's refusal is caught as before: one
  compaction, then the call again. Elsewhere nothing changes.
- **A server of your own** can say it refuses too long a request with
  `refuses_overlong_requests = true` in `models.toml`.
- **Versions renumbered.** A release carries its checkpoint's number again (0, then the
  checkpoint): the releases once called 0.7.1 to 0.7.6 are 0.7.0.1, 0.7.1, 0.7.2, 0.7.3,
  0.7.3.1 and this one, 0.7.3.2. If `zero --version` says 0.7.4, 0.7.5 or 0.7.6, run
  `git -C ~/Applications/zero fetch --prune --prune-tags --tags origin`, then `zero update`.

## 0.7.3.1

Compacting when you ask is now a clean break, and the automatic compaction keeps more.

- **`/compact` always writes a handoff**, with words or without, and keeps only the newest
  step as it is, so a wrong turn you want gone is summed up rather than kept whole. ZERO's
  own `compact` does the same. With nothing older than the newest step, the transcript says
  so plainly instead of reporting a failure.
- **The automatic compaction prefers clearing.** Near its line, ZERO first moves old command
  outputs to files; that alone is now the compaction whenever it leaves room for the next few
  steps at the session's own pace, measured from the model server's own counts. A handoff,
  which can't be undone, comes less often.
- **Fix.** A handoff no longer takes an answer ZERO already gave for one still to give.

## 0.7.3

A compaction told what to keep: you, or ZERO itself, can say what a handoff must hold when
a long session's context is compacted.

- **`/compact` takes words.** `/compact keep the failing test names and the plan` in the full
  screen, `zero session compact ID keep …` on the command line, or `instructions` on the
  protocol's `session/compact`: the handoff ZERO writes keeps what you named, beside its
  usual parts (your requests, constraints, what was done, the state, the next step), never
  in their place. Words always get a handoff, and ZERO reads them again where the compaction
  happened.
- **ZERO can say it too.** Its `compact` tool takes the same words, so before a compaction
  it can name what the work ahead needs.
- **A result near the line is no longer shown with nothing.** A command's output that comes
  as the context nears its line now gets its share of the room a compaction leaves (one
  comes before the next step there), so ZERO sees it whole, or its start and end, where it
  used to see only where it was saved.
- **Fixes.** A compaction between turns is no longer followed by a second one at once on a
  model server that doesn't count tokens; a file read is no longer refused as too big for a
  fresh context when it fits.

## 0.7.2

The log is the truth: what ZERO keeps of a session's state is rebuilt from the session's own
log, so a server killed at any moment comes back right.

- **A crash anywhere comes back right.** Each session's log is written first; the database
  beside it records how far it has followed, and at start a session whose log is ahead is
  brought back in step from the log. Tried by killing ZERO at each write it makes in a turn,
  an ask, rounds of work, a subagent and a fork: every message answered once, nothing left
  waiting.
- **A subagent is started whole**, its session, its record and its task together; **a round
  of work ends once**, even when ZERO stopped in the middle of its end; **a background job's
  end is always recorded**, so a job that is gone no longer keeps the server up.
- **An ask keeps its reason apart.** The plain transcript, `zero -p` and the session list show
  the agent's own reason for asking, never the rule's words before it.
- **Your sessions come along.** The database upgrades when the server starts; a session's log
  moves to format 4 the next time it runs, its original kept beside it. What ZERO reads in
  your sessions stays word for word the same.

## 0.7.1

Data, not words: ZERO's parts now hand each other data where they used to read each other's
sentences, so a change of wording can't break a part that depends on it.

- **A command's result carries its data.** Beside the text ZERO reads, a command's exit code,
  its time and the background job it started or moved to travel as fields, as MCP tools do
  with structured content. The full screen draws a command's block from them, the judge reads
  them, and the protocol's schema publishes what an app reads of a tool call's record
  (`record.tool` in `protocol/schema.json`).
- **Your older sessions come along.** A session's log moves to format 3 the next time it runs,
  its original kept beside it as `<id>.format-2.jsonl`; reading it changes nothing on disk.
  What ZERO reads in it stays word for word the same.
- **ZERO's texts name a tool only where the session has it**: a rewind's or a fork's note no
  longer points at session search in a session without it.
- **Smaller:** the use counters a long-running server keeps for its sessions' logs have a cap.

## 0.7.0.1

The fixes: what a full read of ZERO's code found, and ZERO kept out of the way of slow work.

- **A "don't ask again" covers only the command it names.** What a remembered permission matches
  is now read as bash runs it: a comment, a substitution inside quotes, `eval`, `sudo` or `ssh`,
  a sed script that runs or writes, `./ls` for `ls`: each of these asks again.
- **The sandbox hides more.** Cloud, registry and other agents' logins, browsers' profiles and
  keyrings are hidden from sandboxed commands, and the machine's sockets (docker's, the system
  bus, X11's, those in `/tmp`) are out of reach. A command can't plant a ZERO.md or a git hook:
  where none exists, an empty stand-in is held read-only while it runs.
- **`zero doctor`** checks what ZERO needs from the machine: Python, a command started in the
  sandbox, and anything in your files this ZERO doesn't know. It runs at install; when the
  sandbox can't start a command, the result says why (on Ubuntu, often its user-namespace
  limit; the README shows the fix). The README says ZERO runs on Linux, and it runs on
  Python 3.11.
- **Your files across versions.** Each file ZERO keeps states its format. A setting or line this
  ZERO doesn't know is reported and kept, never a refusal: one unknown key no longer refuses all
  of `config.toml`, and lines an older `models.toml` holds no longer stop every session. A file
  is written whole or not at all, one writer at a time; a link to it (a dotfiles repository's)
  stays a link.
- **The full screen never waits on the server.** It keeps drawing and taking keys while the
  server is slow; a long delete or rewind in one session no longer holds the others, and a
  client that falls behind follows again from the log. Long sessions read only their new lines.
- **What ZERO knows of its sandbox:** a command there sees only its own processes, and ZERO's
  own folder reads as missing there; SOUL.md and your ZERO.md stand under their names in its
  system message.
- **A subagent past its estimate is told which half it passed**, the time or the tokens, not
  the whole estimate.
- **Session search ranks the words said above the output that repeats them.**
- **Smaller fixes:** a request the server didn't answer isn't sent twice; a question answered
  just before a crash no longer waits forever; a crash no longer reads as your interruption; a
  stop between two rounds stays stopped; a refused call isn't counted as one that ran; `zero
  --mode auto new` opens the full screen; `zero mcp serve`'s `zero_wait` follows a dropped
  watch; a job's output is never shown twice; `[deepseek] # mine` in `models.toml` stays one
  table; the build's hash covers only what ships, so starting is quicker.

## 0.7.0

The first release of ZERO's seventh generation: the sixth carried over as it was, ahead of the
redesigns and new features to come.

- **The instruction files' room holds on every path.** SOUL.md, your ZERO.md and a project's
  ZERO.md files share one room in ZERO's system message; when one was cut to fit, the blank line
  after it could take the room one or two characters past its size, depending on the folders'
  names. That line is now counted.

## 0.6.9

The last release of ZERO's sixth generation (memory and long work): fixes, and ZERO's version.

- **ZERO knows its version.** Its system message says which release it is, read from this
  CHANGELOG's newest section, and `zero --version` prints it.
- **One default instruction fewer.** A new ZERO.md no longer starts with "A claim holds when it
  was read, not recalled…": it sent ZERO to check on disk what its system message had just told
  it. Your own ZERO.md is untouched; the line is yours to keep or remove.

- **The server's rest is exact.** A turn that has just ended still says its last word (that the
  session has settled, then the start of a message that came meanwhile); the server now counts it
  as busy until then, so `zero server status` and the idle exit never see it at rest a moment early.
- **Every instruction file is named.** SOUL.md, your ZERO.md and a project's ZERO.md files share
  one room in ZERO's system message; a file found once that room is spent is now always named,
  with the line where it starts. Before, it was named or left out depending on a few characters.
- **A subagent's report reaches its parent once.** When a subagent ends its turn by repeating a
  report it already sent, the answer isn't passed up again.
- **No error at quitting the full screen.** Its redraw timer could fire while the screen was
  being taken down, and print an error as `zero` closed.

## 0.6.8.1

The balance between the means: what ZERO weighs a pick with.

- **Time or tokens, your call:** a new setting, `/settings` → Default → "Time or tokens"
  (`time_or_tokens`): `fewest_tokens`, `balanced` (as shipped) or `least_waiting`. ZERO reads it as
  a fact about you, in its system message, for a pick between two means that would reach the same
  result, such as working alone or through subagents. An open session reads a change at its next
  turn.
- **A note when the context has grown:** each time a session's context passes another 100,000
  tokens, ZERO is told its size, what every step now counts for reading it again, and what the turn
  has used so far. Numbers only; what to do with them is ZERO's to weigh (a compaction, rounds,
  fewer steps). `context_note_every_tokens` in Advanced → Limits; 0 turns it off.
- **`wait`, a new tool:** ZERO can hold a turn until a notice or a message from you comes, or some
  minutes pass, with no call to its model meanwhile. Where it used to `sleep` or end its turn and
  answer twice, it now holds the turn and gives one reply.
- **`usage` shows the context's size now** and what a step counts for reading it again.
- **What ZERO is told of its means, reworded:** a turn's line says what a step of a long turn
  counts, with a number at the model's own weights; rounds start at any step of a turn, which
  becomes round 1, and a round's steps read that round's work alone; what a subagent reads is new
  input, counted in the session's use.

## 0.6.8

ZERO picks the means: what it reads of them, and the unit it counts in.

- **ZERO counts in weighted tokens, not dollars.** Its use and its estimates (work in rounds, a
  subagent) are in tokens weighted by the model's price: a new input token counts 1, one read from the
  cache and an output token what the price makes them against it (on DeepSeek Flash, 1/50 and 4). A
  model with no price counts every token 1. `start_rounds` and `start_subagent` take `weighted_tokens`
  in place of `dollars`, and `usage` shows the tokens by kind with their weighted total. The unit is
  the same on every server, a local one included.
- **Your screens keep dollars:** a run's line in the status line and in `zero rounds list` shows
  dollars on a model with a price, and weighted tokens on one without.
- **No price to type.** Each release fills in the prices of the catalog's servers' models from
  models.dev's public list (`model/prices.toml`). A price in the catalog or in your `models.toml` comes
  first; a model none of them knows has no price and works as before.
- **What ZERO is told of its means, as facts:** each step of a turn reads the whole conversation again;
  the calls of one step run one after another, in the order written; a background job runs side by
  side with the turn with no call to the model, and its notice reaches a running turn at its next step
  or starts a new one; what a compaction, a round and a subagent each cost. When a notice is read is
  said once, in the system message.
- **The means side by side, in the system message:** it now says that how a task is carried out is
  ZERO's to pick, and what each means it has is for: this turn, a background job, rounds, subagents
  (a part of the work done outside the conversation, so the session's own context stays small, and
  several side by side). A session reads only the means it has. On a research task over six code
  bases ZERO then started six subagents unasked, where before it worked alone.
- **Fixed:** a new session's system message is written at its first turn, with what its server has
  handed it by then.
- **Older runs and subagents read as before:** one estimated in dollars keeps them on your screens, and
  ZERO reads its minutes.

## 0.6.7.1

Every limit live.

- **A change to any limit applies at once**, to every session, open ones too, at its next use: in
  `/settings` → Advanced → Limits, with `zero setting set`, or in config.toml by hand. A call already
  running keeps the number it started with. Sessions no longer keep the limits they were made with;
  older sessions read as before.
- **New under Limits:** "Percent of the room left a result may take" (15), "Tokens kept for the next
  reply" and "Tokens kept for a compaction's handoff" (8,000 each), "Characters of a command's output
  kept" (16,000,000), "Megabytes of a file the file tools take" (10), "Seconds before the same call
  runs again" (30), "Megabytes a web fetch reads" (5), "Redirects a web fetch follows" (5), "Results a
  web search may ask for" (1 to 50), three for session search (10 results, 5 records on each side, 32
  words in a snippet, at most 64), and "Characters of each message a rewind's or fork's note shows"
  (200).
- **Shown now:** "Seconds before the first retry" of the model, and "Seconds the consent check may
  stay silent", moved from Server to Limits.
- **The fallback model applies at once too**, at a call that fails.

## 0.6.7

Subagents.

- **ZERO starts subagents itself** when it judges one fits: a session of its own under the one it works
  in, which works on a task while ZERO goes on. It starts either from a copy of the conversation so far
  (its first call from the server's cache) or with the task alone, written in full. Its task asks it to
  report back when it has finished, and its report arrives in ZERO's session as a notice. If it ends a
  turn without sending its answer, the answer is passed up; if one of its turns crashes, fails or is
  stopped, ZERO is told. A subagent may start its own, two levels deep by default (`/settings` →
  Advanced → Limits → Subagents depth; 0 turns subagents off, and a change applies at once to every
  session, open ones too); there's no limit on how many.
- **Messages between sessions:** ZERO's sessions message each other by id, as notices. A message never
  answers a question.
- **Nobody watches a subagent:** in default mode it runs with the judge answering what would have
  asked you, its goal your words anywhere in the session tree; other modes run as they are. Work in
  rounds does the same: in default, a round's questions go to the judge instead of being refused.
- **`subagents`** lists the session tree with each session's state and use, reads one's last answer,
  and stops a turn of one below; **`usage`** says what this turn and this session have used (time,
  tokens, the cache's share, dollars), each subagent against the estimate it was started with.
- **No spend limit stops a turn any more:** the standing amount (2 h, $2.00) and its question are gone.
  ZERO states an estimate when it starts rounds or a subagent, and is told once if the work passes it.
  It knows its model's price for that: the system prompt names it (a local model "has no price").
  Amounts under a cent read to their first two figures ($0.0017), not $0.00.
- **The session list** marks a subagent's session "(subagent)". **`zero -p --json`** and the MCP
  server's `zero_wait` say what still runs once a session settles (jobs, rounds, subagents), and
  `zero -p` prints a "Still running" line.
- **Your sessions:** the state database gains `sessions.started_by` and a subagents table; work in
  rounds keeps its runs, their budget columns renamed to estimates (a copy of the database kept beside
  it). Old settings for the spend limits (`standing_budget_*`, `max_seconds`, `max_steps`) are ignored
  and dropped at the next save.

## 0.6.6

Rewind and fork.

- **`/rewind`:** go back to before one of your messages (a list of them, newest first); its text comes
  back in the input, to send as it is or changed. What came after is left behind: out of ZERO's
  context, kept in the log. ZERO reads a note in its place: your messages there, the calls that ran
  (by tool), the files its file tools wrote and the jobs still running from there, and that files and
  memory stay as they are. The conversation goes back, not the machine. Not while a turn runs or
  waits, a message waits in line, or a rounds run is open. A mode, model or folder you changed on the
  part left behind stays as you left it.
- **`/fork`:** a new session from this one's conversation, up to one of your messages or all of it, in
  the same folder with the same mode and model; marked "(fork)" in the list. Its first call starts from
  the server's cache, as the session it came from would; ZERO reads a note saying whose fork it is,
  that files and memory are shared, and, when that session had gone on past the point, what it did
  there. The session it came from goes on untouched.
- **A job's notice from a part left behind** still comes, saying it started there.
- **Session search** marks what it finds on a part left behind, and finds a fork's copied records once,
  in the session that wrote them.
- **`zero session rewind ID [SEQ]`** and **`zero session fork ID [SEQ]`** (no SEQ: your messages to pick
  from; for a fork, all of it).
- **Your sessions:** old logs read as they are and stay unchanged; the state database gains two
  columns in place (`sessions.parent`, `jobs.started_from_seq`).

## 0.6.5

Session search.

- **ZERO searches its sessions:** a new tool, `session_search`, searches what every session's log holds,
  running or done, this one included: your messages, notices, ZERO's replies (not its thinking), each
  command or tool call with its result as ZERO saw it (not its own searches, which repeat the rest),
  and compaction handoffs. Ask "what did we find about X last week?" and ZERO looks. It gives the
  sessions that match, best first, each with its best record; then one session's matching records;
  then the records near one, whole.
  - A record matches when it holds every word, each whole, case and accents aside; when none does, the
    records holding any of the words come instead, and the reply says so. `OR` between words finds any,
    `"a phrase"` a phrase, `word*` the words that start with it.
  - The session that searches comes after the others: its own question always matches.
  - It reads ZERO's own records and changes nothing, so it runs unasked in every mode, readOnly too.
- **`zero session search WORDS`:** the same search for you and other agents, with `--session ID`,
  `--around N` (the records near record N; `--session` alone reads its first ones), `--count N` and
  `--json`; its lines are `session list`'s, each with its best record.
- **The index** is `~/.zero/state/session_search.sqlite`, made from the logs at the first search and
  caught up at each one; the logs stay the record, so a missing or broken index is made again. Nothing
  else in `~/.zero` changes.

## 0.6.4.1

Notices labelled once.

- **A notice is tagged, not explained each time:** a background job's or work in rounds' message to ZERO
  now comes as `<notice from="job">…</notice>` (or `from="rounds"`), and what a notice is, ZERO's and not
  yours, is said once in ZERO's instructions. Before, every notice ended with a sentence saying so, some
  25 tokens each. Sessions from before keep their notices' words, tagged.
- **Three facts ZERO was missing:**
  - In the sandbox each command has a loopback of its own, so a server one command starts can't be
    reached from another; the sandbox's line says so.
  - After ZERO restarts, a session's scratch folder (`$TMPDIR`) is a new, empty one; the session's next
    turn is told its path.
  - When you stop a turn, ZERO is told which of the session's background jobs still run.

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
