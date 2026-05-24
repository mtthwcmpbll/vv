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

- **List existing sessions** — pick a worktree, then choose to **resume** it
  (re-attach to its tmux session, or start a fresh one) or **delete** it.
  Deleting a worktree with uncommitted changes or unpushed commits warns you
  first and lets you cancel.
- **Start a new session from an existing repo** — pick an already-cloned repo
  and start a new worktree session for it.
- **Add a new repo** — paste a git URL and start a session from it.

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
vv                                     # interactive menu
```

## Configuration

| Variable         | Default                | Purpose                                |
| ---------------- | ---------------------- | -------------------------------------- |
| `WORKSPACES_DIR` | `~/.vv/workspaces`     | Primary clone of each repo             |
| `WORKTREES_DIR`  | `~/.vv/worktrees`      | Per-session worktrees, grouped by repo |
| `VV_CONFIG`      | `~/.vv/config.toml`    | TOML config file (`agent`, `ask` keys) |
| `VV_AGENT`       | `claude`               | Agent CLI to launch (`--agent` wins)   |
