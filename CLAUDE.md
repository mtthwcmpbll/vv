# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

This is a `uv`-managed Python project (Python >= 3.14).

```sh
uv sync                 # install dependencies (incl. dev group) into .venv
uv run vv ...           # run the CLI during development
uv run pytest           # run the unit test suite
uv tool install .       # install the `vv` command globally
```

Unit tests live in `tests/` (no linter is configured yet). They run real
`git` against throwaway repos (the `remote_repo` fixture in `conftest.py`) and
stub `tmux` / `questionary` / `PATH` rather than touching real sessions. To
verify changes end-to-end, run `vv` against a local repo used as a fake remote:

```sh
TMP=$(mktemp -d); git init -q -b main "$TMP/remote"
git -C "$TMP/remote" -c user.email=t@t -c user.name=t commit -q --allow-empty -m init
WORKSPACES_DIR="$TMP/ws" WORKTREES_DIR="$TMP/wt" uv run vv "$TMP/remote"
```

(`attach` will fail with "not a terminal" when run without a TTY — that is
expected; the clone/worktree/tmux session are still created.)

## Architecture

`vv` creates disposable coding sessions: each is a fresh git **worktree**
running inside its own **tmux** session with an **agent CLI** launched
(`claude` by default; configurable). The point is detachable, rejoinable
sessions.

The package is `vv/`, with a single Typer command exposed as the `vv`
console script (`vv.cli:run`).

The **worktree is the session**: a worktree exists whether or not a tmux
session is currently live for it. Resuming a worktree attaches to its tmux
session if one is running, or starts a fresh one otherwise.

Four flows, all ending in `_resume_worktree()`:

- **`vv <repo_url>`** → `cli._start_from_url()`: clone into
  `WORKSPACES_DIR/<repo>` (or fetch if already present), then
  `_new_worktree_session()`.
- **`vv --chat`** (a.k.a. `-c`) → `cli._new_chat_session()`: create an empty
  directory under `WORKTREES_DIR/_chats/<name>` (no git involved), then
  `_resume_worktree()`. For persistent agent conversations that don't need
  version control. Cannot be combined with a repo URL.
- **`vv`** (no args) → `cli._interactive_menu()`: a `questionary` menu to
  list existing sessions, start a worktree from an already-cloned repo, add a
  repo by URL, or start a chat-only session.

`_new_worktree_session()` picks a random collision-free word
(`names.random_name()`, excluding existing tmux sessions, git branches, and
worktree dirs), creates a worktree on a new branch of that name off the remote
default branch, then calls `_resume_worktree()`.

`_resume_worktree()` is the core: given a worktree name + path + agent, it
attaches to the live tmux session of that name if one exists, otherwise starts
a detached session rooted at the worktree, sends the agent command to it, and
attaches. `_list_worktrees()` enumerates worktrees across all cloned repos (via
`git_ops.list_worktrees()`, filtered to the per-repo `WORKTREES_DIR` location)
**plus chat-only sessions** under the `_chats` sentinel namespace, to feed the
"list existing sessions" menu. Chat sessions surface in that listing as
`(_chats, name, path)` tuples; the sentinel string is `cli.CHATS = "_chats"`.

The "list existing sessions" menu (`_menu_list_sessions()`) offers each chosen
worktree a **resume** (→ `_resume_session()`) or **delete** (→
`_delete_session()`) action. Deletion first checks `git_ops.is_dirty()` and
`git_ops.unpushed_count()`; if either flags work that would be lost it requires
a `questionary.confirm()` before proceeding. It then kills any live tmux
session and runs `git_ops.remove_worktree(force=True)` +
`git_ops.delete_branch(force=True)` — so a deleted worktree frees its name for
reuse. Chat sessions branch through `_delete_chat()` instead: no git ops, but
the user is still warned if the directory is non-empty before `shutil.rmtree`.

The **agent** is just the command typed into a fresh session, so anything on
`PATH` works. It is resolved once in `cli.main()` with precedence
`--agent` flag / `$VV_AGENT` (both via Typer's `envvar=`) > config file's
`agent` key > `agents.DEFAULT_AGENT`. The
interactive menu's new-session flows call `_pick_agent()` (a `questionary`
picker of `agents.installed_agents()`); resuming a *dead* worktree also picks,
a *live* one just re-attaches. The `vv <repo_url>` flow never prompts.

### Module responsibilities

- `config.py` — resolves `WORKSPACES_DIR` / `WORKTREES_DIR` and the `VV_CONFIG`
  TOML file (all env-overridable; default under `~/.vv/`). Also exposes
  `chats_dir()` (= `WORKTREES_DIR/_chats`) for chat-only sessions. Parses the
  config file (`configured_agent()`); raises `ConfigError` on malformed TOML.
- `agents.py` — `DEFAULT_AGENT`, the `KNOWN_AGENTS` list seeding the picker,
  and `PATH` detection (`installed_agents()`, `is_installed()`).
- `git_ops.py` — `git` CLI wrappers; raises `GitError`.
- `tmux_ops.py` — `tmux` CLI wrappers; raises `TmuxError`.
- `names.py` — curated single-word name list + collision-avoiding picker.
- `cli.py` — Typer app, flow orchestration, interactive menu.

### Conventions to preserve

- All git/tmux interaction shells out to the CLIs (no library bindings);
  failures surface as `GitError` / `TmuxError` (and `config.ConfigError` for a
  bad config file), caught centrally in `cli.main()`.
- The worktree name is used as the branch name *and* tmux session name — keep
  `names.WORDS` entries valid as both (no `.`, `:`, `/`, or spaces).
- `tmux send-keys` targets must use the `=name:` form (trailing colon) for an
  exact-match session→pane target; `=name` alone fails with "can't find pane".
- `attach()` uses `switch-client` when already inside tmux (`$TMUX` set) and
  `execvp` to hand over the terminal otherwise — do not replace this with a
  blocking `subprocess.run`.
- vv-created tmux sessions are stamped with the `@vv` session option
  (`tmux_ops.VV_TAG`); `list_sessions(vv_only=True)` filters on it. The
  unfiltered `list_sessions()` feeds collision avoidance, which must consider
  *all* tmux sessions, and the "running" annotation in the resume menu.
