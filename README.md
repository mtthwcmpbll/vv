# vv

Quickly spin up disposable, detachable coding sessions. Each session is a fresh
git **worktree** running inside its own **tmux** session with an **agent CLI**
already launched (`claude` by default) — so you can disconnect, leave it
running, and rejoin later.

## How it works

Given a git repository URL, `vv`:

1. Clones the repo into `WORKSPACES_DIR/<repo_name>` (skipped if already cloned;
   an existing clone is fetched instead).
2. Creates a new worktree at `WORKTREES_DIR/<repo_name>/<worktree_name>` on a
   fresh branch. The worktree name is a random memorable word (e.g. `falcon`).
3. Starts a detached tmux session named after the worktree, `cd`'d into the
   worktree directory, and launches your agent CLI.
4. Attaches you to the session (or switches to it if you are already in tmux).

Run with no arguments for an interactive menu:

- **Live session board** — every session grouped by whose turn it is (see
  below). Also `vv --watch`.
- **List existing sessions** — pick a worktree, then choose to **resume** it
  (re-attach to its tmux session, or start a fresh one) or **delete** it.
  Deleting a worktree with uncommitted changes or unpushed commits warns you
  first and lets you cancel.
- **Start a new session from an existing repo** — pick an already-cloned repo
  and start a new worktree session for it.
- **Add a new repo** — paste a git URL and start a session from it.
- **Create a new GitHub project** — make a brand-new repo and session it in one
  go. Pick one of your GitHub **template** repos (type to filter) or "Empty
  repository", choose the account or organization to own it, name it, and pick
  its visibility; `vv` creates it with `gh`, clones it, and drops you into a
  session exactly as if the repo had already existed. Needs the
  [`gh` CLI](https://cli.github.com) installed and logged in.

## The session board (`vv --watch`)

Past a handful of sessions the question stops being *what is this one?* and
becomes **which one is waiting on me?** `vv --watch` answers that: a live board
that groups every session into a lane by whose turn it is, and re-reads the
world on a timer so a glance is always current.

```
◆ Needs you              2   ▸ Working            1   ◇ In review          1
──────────────────────────   ──────────────────────   ──────────────────────
❯ ╭────────────────────────╮   ╭────────────────────╮   ╭────────────────────╮
▌ │ ◆ Adding the state axis│   │ ▸ Refactoring cards│   │ ◇ Rate limiter     │
▌ │   ↳ Should I also upda…│   │ swift-heron · vv/… │   │ lucky-ibis · api/… │
▌ │ brave-falcon✱ · vv/br… │   │ ○ no open PR  2m   │   │ ○ PR #42 ✓ passing │
▌ ╰────────────────────────╯   ╰────────────────────╯   ╰────────────────────╯
```

Five lanes, ordered by how much they want from you:

| Lane | Means |
| --- | --- |
| `◆` **Needs you** | The agent stopped talking and it is your move |
| `✕` **Stopped** | The tmux session is alive but the agent has exited |
| `▸` **Working** | The agent is producing output right now |
| `◇` **In review** | Pushed, PR open — CI and reviewers have it |
| `▹` **Idle** | Nothing pending |

Two things make this more than a list. A session in **Needs you** shows the
agent's own last words (`↳ Should I also update the tests?`) — what it wants
beats a summary of what it was doing. And **Needs you expires**: an agent that
stopped an hour ago is waiting on you, one that stopped last Tuesday is
abandoned, so after `stale_after` (24h by default) a session drops into Idle
where the stale sweep can clean it up. The lane holds today's work, not
everything you ever started.

Keys: `↑↓` move, `←→` jump lane, `enter` resume, `x` delete, `r` refresh now,
`q` quit. The board lays lanes out side by side when the terminal is wide enough
and stacks them when it is not. It never runs an agent — summaries are served
from the cache the session list writes — so leaving it open all day is cheap.

The same states drive the dot on every session card, and the session list is
ordered by them too, so it reads as a queue rather than a pile.

## Titles and labels

Two manual levers for keeping many sessions straight: a **title** you write
yourself, and **labels** — a customer name, a ticket, a note. Both show on the
session cards in the interactive menu:

```
╭────────────────────────────────────────────────────────╮
│ ▸ Acme onboarding                                      │  <- your title
│   Wiring the notes store into the session cards        │  <- generated summary
│   #Big Customer  #urgent                               │  <- your labels
│ alpha · repo/alpha                                     │
│ ○ PR #12 ✓ passing                          10m ago    │
╰────────────────────────────────────────────────────────╯
```

A title doesn't replace the generated summary — it sits above it.

```sh
vv --title "Acme onboarding"           # title the session you're in
vv -t ""                               # clear the title
vv --label acme                        # tag it
vv -l acme -l "needs review"           # repeatable, spaces are fine
vv --label=-acme                       # a leading '-' removes the label
vv -t "Acme" -l acme --name falcon     # annotate another session by name
vv -t "Acme" https://github.com/o/r.git  # annotate the session being created
```

Run from inside a session (or any subdirectory of it) and `vv` annotates that
session; otherwise pass `--name`. Titles and labels live in
`WORKTREES_DIR/.session-notes.json` and are forgotten when the session is
deleted.

## Bundled skills

`vv` ships a `pr` skill — commit, rename the branch to `<type>/<short-summary>`
so the remote branch reads well, push, open the PR. `vv --skills` installs it
into every supported agent tool it finds on your machine:

```sh
vv --skills
```

| Tool | Installed to |
| --- | --- |
| Claude Code | `~/.claude/skills/` (or `$CLAUDE_CONFIG_DIR`) |
| Cursor | `~/.cursor/skills/` |
| GitHub Copilot | `~/.copilot/skills/` |
| Codex | `~/.codex/skills/` (or `$CODEX_HOME`) |
| Antigravity | `~/.gemini/config/skills/` |

A tool is only touched when its config directory already exists, so `vv` never
invents config dirs for tools you don't use — the rest are listed as skipped.
Re-running is safe: unchanged copies are left alone, and a copy you have edited
is listed and confirmed before it gets overwritten. Restart a tool to pick up
its new skills.

## Agent CLI

`vv` launches `claude` by default, but any agentic CLI on your `PATH` works
(`codex`, `gemini`, `copilot`, …). Pick one per run with `--agent` or the
`VV_AGENT` environment variable, or set a persistent default in the config
file:

```sh
vv --agent codex https://github.com/owner/repo.git
VV_AGENT=codex vv                       # same, via the environment
```

```toml
# ~/.vv/config.toml
agent = "codex"
```

Precedence is `--agent` flag → `$VV_AGENT` → config file → `claude`. The
interactive menu prompts you to choose, listing the known agents found on
your `PATH`.

### Permission prompts

Each session runs in a disposable worktree, so `vv` launches agents in
**bypass mode** — their permission/approval prompts are turned off. Use
`--ask` to launch with the agent's normal prompts instead, or set
`ask = true` in the config file (`--ask` / `--no-ask` override it per run).

```sh
vv --ask https://github.com/owner/repo.git   # keep the agent's prompts
```

> Only Claude Code's bypass flag is verified. The flags for the other agents
> live in `BYPASS_FLAGS` in `vv/agents.py` and are best-guesses — check each
> CLI's `--help` and correct them.

## Install

Requires `git` and `tmux` on your `PATH`, plus at least one agent CLI.

```sh
uv tool install .      # install the `vv` command
# or, during development:
uv run vv
```

## Usage

```sh
vv https://github.com/owner/repo.git   # clone + new worktree session
vv git@github.com:owner/repo.git       # scp-style URLs work too
vv --agent codex                       # choose the agent CLI for this run
vv -t "Acme onboarding" -l acme        # title/label the session you're in
vv --watch                             # live board: who needs you
vv                                     # interactive menu
```

## Configuration

| Variable         | Default                | Purpose                                |
| ---------------- | ---------------------- | -------------------------------------- |
| `WORKSPACES_DIR` | `~/.vv/workspaces`     | Primary clone of each repo             |
| `WORKTREES_DIR`  | `~/.vv/worktrees`      | Per-session worktrees, grouped by repo |
| `VV_CONFIG`      | `~/.vv/config.toml`    | TOML config file (`agent`, `ask` keys) |
| `VV_AGENT`       | `claude`               | Agent CLI to launch (`--agent` wins)   |

Board timing lives in a `[board]` table, and the two windows also decide the
dot on every session card:

```toml
# ~/.vv/config.toml
[board]
refresh = 10          # seconds between board refreshes
active_window = 60    # quiet longer than this and a session stops being "working"
stale_after = 86400   # quiet longer than this and it stops asking for attention
```
