<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/media/zero-logo-dark.svg">
    <img src="docs/media/zero-logo-light.svg" alt="Z.E.R.O." width="360">
  </picture>
</p>

<h3 align="center">A general purpose agent.</h3>
<p align="center"><sub>Zealous Efferent Response Operator</sub></p>

<p align="center">
  <a href="#install"><b>Install</b></a> · <a href="#use">Use</a> · <a href="#for-other-agents">For other agents</a> · <a href="#update">Update</a> · <a href="CHANGELOG.md">Changelog</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Linux-007C83?style=flat-square&logo=linux&logoColor=fff" alt="Linux">
  <img src="https://img.shields.io/badge/Python-3.11%2B-6A55D8?style=flat-square&logo=python&logoColor=fff" alt="Python 3.11 or newer">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-555?style=flat-square" alt="MIT license"></a>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/media/zero-screen-dark.svg">
    <img src="docs/media/zero-screen-light.svg" alt="ZERO's TUI: a question, the command it ran and its answer" width="860">
  </picture>
</p>

- **Any model:** any server that speaks the OpenAI chat API. ZERO ships a catalog (DeepSeek, OpenAI, Gemini, Qwen, Kimi, GLM, OpenRouter, Ollama) and takes servers of your own; no model is built in.
- **Many sessions:** each with its own workspace and history, opened, switched, renamed or archived from any terminal.
- **Permissions you choose:** five modes, from read-only to never asking, a sandbox (bubblewrap), and a judge that answers in the auto modes, deciding only after it gives its reason and takes a second look at it.
- **One host:** it runs every session. Turns, background jobs and waiting questions go on after the terminal closes, and every app reaches the host the same way.
- **It trusts the model:** what the model reads states facts, never guardrails; the limits live in code; it is built for the models to come.

> [!NOTE]
> ZERO is in development (versions below 1.0): anything may change between releases. Each release is one checkpoint of its development, or a fix between two; [CHANGELOG.md](CHANGELOG.md) says what each brought.

## Install

```bash
git clone https://github.com/jody0731/zero.git ~/Applications/zero
~/Applications/zero/install.sh
```

The installer checks what ZERO needs, checks out the newest release, makes a Python environment in the clone (`.venv/`), installs the requirements and puts the `zero` command in `~/.local/bin` (`--bin DIR` puts it elsewhere). Nothing starts at login.

<details>
<summary><b>What it needs</b></summary>

- Linux, with `git`, Python 3.11 or newer (made and tested on 3.14) and its `venv` module
- [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`): ZERO's sandbox for the commands it runs
- a model: an API key for a provider ZERO knows (DeepSeek, OpenAI, Gemini, Qwen, Kimi, GLM, OpenRouter), or a server of your own that speaks the OpenAI chat API, such as Ollama or llama-server

</details>

## Use

### Connect a model

The first `zero` with no model opens **Connect a model**. Pick a provider and paste its API key, or choose **Your own server…** and give its URL, a key if it takes one, a model it serves and, where the server doesn't say, its context window. ZERO makes a one-token test call before it saves anything, and the first model connected becomes new sessions' default. Later, `/settings` → **Models and keys…** connects another model, takes a new key, tests a server or removes it.

<details>
<summary><b>The same, by hand</b>: ZERO's files in <code>~/.zero/</code></summary>

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

Its models are then `myserver/<model>`. The catalog ZERO ships, with every field explained, is [model/catalog.toml](model/catalog.toml); a line in models.toml under the same name changes any of it.

</details>

### TUI

```bash
zero                 # this folder's latest session, or a new one on your first line
zero new             # a new session here
zero -r              # the session list
zero -p "a question" # one question, for scripts
```

Inside, `/help` lists the keys and `/` the commands; `/model` sets the session's model and its thinking; `/settings` sets this session, what new sessions start with, the theme, the models and keys and the key bindings, and in **Dev** the judge, the limits and the host.

### CLI

Everything a caller does has a command, named as its message: a family, then the action.

```bash
zero session send "a task" --cd ~/project --json   # one result object when it settles
zero session list                                  # read, watch, stop, continue, rename, archive, delete, set
zero job list                                      # job stop JOB
zero grant list ID                                 # grant forget ID GRANT
zero model provider list                           # probe, connect, disconnect
zero setting set max_steps 80                      # setting list
zero host status                                   # whether the host runs and what keeps it busy
```

<details>
<summary><b>Names, input, output and exit status</b></summary>

- A session or a job is named by its id or the start of it.
- A long text can come from stdin (`-`); a key only does (`--key-stdin`), never as an argument.
- `--json` prints the host's reply as one JSON object; `session send --json` prints the session, its outcome, the answer, a waiting question, the tokens and the seconds.
- The exit status is 0 when it worked, 1 when not, 2 when a question waits: answer it with `zero session send TEXT --session ID`, or pick one of its choices with `--choice yes|session|saved|no`.
- `zero -p TEXT` is short for `zero session send TEXT`.

</details>

## For other agents

`zero mcp serve` serves ZERO as an MCP server on stdio. In Claude Code:

```bash
claude mcp add zero -- zero mcp serve
```

| Tool | What it does |
| --- | --- |
| `zero_start` | hands ZERO a task in a folder, in a mode; returns the session's id at once |
| `zero_wait` | returns the result when the session settles |
| `zero_reply` | answers a waiting question, or sends a follow-up |
| `zero_stop` | stops the running turn |

The mode decides who answers ZERO's questions: in `default` the calling agent does, with `zero_reply`; in `auto` and `auto+` ZERO's judge does.

## Update

```bash
zero update
```

It fetches the releases, checks out the newest, installs the requirements again when they changed and shows what the new release brought. The host picks up the new code once it is idle. It never touches `~/.zero`, and it refuses when the clone has changes of your own.

<details>
<summary><b>If an update goes wrong</b>: the same, by hand</summary>

```bash
git -C ~/Applications/zero fetch --tags
git -C ~/Applications/zero checkout --detach \
  "$(git -C ~/Applications/zero tag --list 'v*' --sort=-v:refname | head -n1)"
~/Applications/zero/install.sh
```

</details>

## Where things are

| Path | What it holds |
| --- | --- |
| `~/Applications/zero/` | the code (this repository) and its `.venv/` |
| `~/.zero/` | everything ZERO keeps, moved by `ZERO_HOME`: your settings (`config.toml`), keys, model servers, `SOUL.md` (who ZERO is) and `ZERO.md` (your instructions and its memory), and in `state/` the sessions and their history |

## Uninstall

```bash
zero uninstall
rm -rf ~/Applications/zero
```

---

<p align="center"><sub>MIT license · <a href="CHANGELOG.md">Changelog</a> · <a href="protocol/schema.json">Protocol schema</a></sub></p>
