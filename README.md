# Z.E.R.O.

Z.E.R.O. (a.k.a. Zealous Efferent Response Operator): a general purpose agent.

- **Model compatibility:** any server that speaks the OpenAI chat API: a catalog ZERO ships (DeepSeek, OpenAI,
  Gemini, Qwen, Kimi, GLM, OpenRouter, Ollama) and servers of your own; no model is built in.
- **Session management:** many sessions at once, each with its own workspace and history, opened, switched,
  renamed or archived from any terminal.
- **Built-in permissions:** five modes, from read-only to never asking, a sandbox (bubblewrap), and a judge that
  answers in the auto modes.
- **Unified hosting:** one host runs every session; turns, background jobs and waiting questions go on after the
  terminal closes, and every app reaches the host the same way.
- **What makes it powerful:** it trusts the model. What the model reads states facts, never guardrails; the limits
  live in code; it is built for the models to come.

ZERO is in development (versions below 1.0): anything may change between releases. Each release is one checkpoint of
its development, or a fix between two; [CHANGELOG.md](CHANGELOG.md) says what each brought.

## What it needs

- Linux, with `git`, Python 3.11 or newer (made and tested on 3.14) and its `venv` module
- [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`): ZERO's sandbox for the commands it runs
- a model: an API key for a provider ZERO knows (DeepSeek, OpenAI, Gemini, Qwen, Kimi, GLM, OpenRouter), or a
  server of your own that speaks the OpenAI chat API, such as Ollama or llama-server

## Install

```bash
git clone https://github.com/jody0731/zero.git ~/Applications/zero
~/Applications/zero/install.sh
```

The installer checks what ZERO needs, checks out the newest release, makes a Python environment in the clone
(`.venv/`), installs the requirements and puts the `zero` command in `~/.local/bin` (`--bin DIR` puts it elsewhere).
Nothing starts at login.

## Use

Every app of ZERO's talks to the same host, which runs the sessions, through one protocol: the terminal (`zero`),
the plain commands, and `zero mcp serve` for other agents. The protocol is JSON lines over the host's socket, its
messages named noun/verb (`session/send`, `job/stop`), each published with its fields and reply as JSON Schema in
[protocol/schema.json](protocol/schema.json), so an app in any language can be built on it.

### Connect a model

The first `zero` with no model opens **Connect a model**. Pick a provider and paste its API key, or choose **Your own
server…** and give its URL, a key if it takes one, a model it serves and, where the server doesn't say, its context
window. ZERO makes a one-token test call before it saves anything, and the first model connected becomes new
sessions' default. Later, `/settings` → **Models and keys…** connects another model, takes a new key, tests a
server or removes it.

It all lands in ZERO's home, `~/.zero/`, where the files can be edited by hand too:

```toml
# ~/.zero/config.toml: new sessions' model, provider/model
model = "deepseek/deepseek-flash"
```

```toml
# ~/.zero/keys.toml (mode 600): each provider's key
[deepseek]
api_key = "..."
```

```toml
# ~/.zero/models.toml: a server of your own, under a name of your choosing
[myserver]
url = "http://localhost:8080/v1"
context = 65536
```

Its models are then `myserver/<model>`. The catalog ZERO ships, with every field explained, is
[model/catalog.toml](model/catalog.toml); a line in models.toml under the same name changes any of it.

### Terminal

```bash
zero                 # this folder's latest session, or a new one on your first line
zero new             # a new session here
zero -p "a question" # one question, for scripts
zero host status     # the host: whether it runs and what keeps it busy
```

Inside, `/help` lists the keys and `/` the commands; `/settings` sets the theme, new sessions' mode and model, the
limits, the models and keys, and the key bindings.

### Plain commands

Everything a caller does has a command, named as its message: a family, then the action.

```bash
zero session send "a task" --cd ~/project --json   # one result object when it settles
zero session list                                  # read, watch, stop, continue, rename, archive, delete, set
zero job list                                      # job stop JOB
zero grant list ID                                 # grant forget ID GRANT
zero model provider list                           # probe, connect, disconnect
zero setting set max_steps 80                      # setting list
```

A session or a job is named by its id or the start of it. A long text can come from stdin (`-`); a key only does
(`--key-stdin`), never as an argument. `--json` prints the host's reply as one JSON object; `session send --json`
prints the session, its outcome, the answer, a waiting question, the tokens and the seconds. The exit status is 0
when it worked, 1 when not, 2 when a question waits: answer it with `zero session send TEXT --session ID`, or pick
one of its choices with `--choice yes|session|saved|no`. `zero -p TEXT` is short for `zero session send TEXT`.

### For other agents

`zero mcp serve` serves ZERO as an MCP server on stdio. In Claude Code:

```bash
claude mcp add zero -- zero mcp serve
```

Its tools: `zero_start` (a task in a folder, in a mode; the session's id at once), `zero_wait` (the result when it
settles), `zero_reply` (an answer to a waiting question, or a follow-up) and `zero_stop`. The mode decides who
answers ZERO's questions: in `default` the calling agent does, with `zero_reply`; in `auto` and `auto+` ZERO's judge
does.

## Update

```bash
zero update
```

It fetches the releases, checks out the newest, installs the requirements again when they changed and shows what the
new release brought. The host picks up the new code once it is idle. It never touches `~/.zero`, and it refuses when
the clone has changes of your own.

If an update ever goes wrong, the same can be done by hand:

```bash
git -C ~/Applications/zero fetch --tags
git -C ~/Applications/zero checkout --detach "$(git -C ~/Applications/zero tag --list 'v*' --sort=-v:refname | head -n1)"
~/Applications/zero/install.sh
```

## Where things are

- `~/Applications/zero/`: the code (this repository) and its `.venv/`
- `~/.zero/`: everything ZERO keeps, moved by `ZERO_HOME`: your settings (`config.toml`), keys, model servers,
  `SOUL.md` (who ZERO is) and `ZERO.md` (your instructions and its memory), and in `state/` the sessions and their
  history

## Uninstall

```bash
zero uninstall
rm -rf ~/Applications/zero
```

`zero uninstall` removes the command; `~/.zero` stays until you remove it.

## License

MIT: see [LICENSE](LICENSE).
