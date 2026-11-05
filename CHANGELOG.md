# Changelog

What each release of ZERO brought, newest first. A release is one checkpoint of ZERO's development, or a fix
between two (a fourth number); versions below 1.0 may change anything.

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
