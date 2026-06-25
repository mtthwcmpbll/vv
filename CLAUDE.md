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

### Remote-launcher mode (cmux)

By default vv runs everything locally. When `mode = "remote"` in the config
file (overridable per-call with `--remote`/`--local`, env `VV_REMOTE`), vv
becomes a thin **launcher**: it does no git/tmux work itself, but opens a native
[cmux](https://cmux.com) **SSH workspace** (a vertical tab) to the configured
server and types `vv` into it. The real worktree/tmux/agent session is created
on the remote, surfaced locally as a cmux tab.

`remote.launch()` is **two cmux calls, not one** (see `remote.py` and
`cmux_ops.new_ssh_workspace`): `cmux ssh <target> --name N --json` opens the
workspace and reads back its `workspace_id`, then `cmux send --workspace <id>`
types the `vv …` command in. We deliberately do **not** pass the command as a
trailing `ssh` argument: cmux skips its remote bootstrap (cmuxd-remote install,
agent notifications, session reconnect) whenever a remote command is present, so
`cmux ssh host -- vv …` would collapse to a plain `ssh host cmd` and forfeit
exactly those integrations. The command is fired immediately after the workspace
opens; the remote shell's input buffer holds it until the SSH session is ready
(type-ahead), which is fine for key-based auth (no interactive password prompt).

It is **transparent** — `cli._launch_remote()` forwards the invocation's intent
to the remote vv: bare `vv` runs the remote's own interactive TUI over SSH,
`vv <url>` / `vv --chat` run the remote create flow. `--local` is always
forwarded so the remote (which has no `[remote]` config of its own) never
recurses.

**Name mirroring is conditional:** when a session is created up front (a URL or
`--chat`, and no explicit `--name`), local vv pre-generates the name via
`remote.gen_name()`, passes it as `--name N`, and titles the cmux tab `N` (via
`cmux ssh --name`) so the tab maps 1:1 to the remote session. Bare `vv` → remote
TUI has no name in advance, so the tab is titled after the host and the remote
names its own sessions. The `--name` flag is consumed by the *remote* vv's local
create flows (`_new_worktree_session` / `_new_chat_session`), which reject an
already-taken name. Config lives in a single `[remote]` table (`host` required;
optional `user`, `port`, `identity`, `ssh_options`, `vv_command`, and the
prompt-readiness knobs `ready_delay` / `ready_timeout` / `ready_interval`)
parsed by `config.configured_remote()`. `ssh_options` are cmux `--ssh-option`
values (`-o Key=Value` passthrough), not raw `ssh` argv; cmux ssh also reads
`~/.ssh/config`, so host aliases/identities work without extra config.

Before typing the `vv` command into the freshly-opened workspace, `remote.launch`
calls `cmux_ops.wait_until_ready()` — a just-connected `cmux ssh` shell isn't
interactive yet, so keystrokes sent mid-startup (the submitting Enter especially)
get swallowed and the command is left typed-but-unrun. It optionally sleeps
`ready_delay` seconds up front (for hosts you *know* are slow to log in; default
`0`), then polls `read-screen` every `ready_interval`s (default `0.4`) up to
`ready_timeout`s (default `20`) until a shell prompt appears (last on-screen line
ends in `$`/`#`/`%`/`>`) or the screen goes quiet (non-empty and unchanged across
two polls). On timeout it warns and sends anyway — no worse than firing blind.

The **agent** is just the command typed into a fresh session, so anything on
`PATH` works. It is resolved once in `cli.main()` with precedence
`--agent` flag / `$VV_AGENT` (both via Typer's `envvar=`) > config file's
`agent` key > `agents.DEFAULT_AGENT`. The
interactive menu's new-session flows call `_pick_agent()` (a `questionary`
picker of `agents.installed_agents()`); resuming a *dead* worktree also picks,
a *live* one just re-attaches. The `vv <repo_url>` flow never prompts.

Agents launch in **bypass mode** (permission prompts off) by default —
`_resume_worktree()` appends a per-agent flag via `agents.with_bypass()`,
looked up in `agents.BYPASS_FLAGS`. `cli.main()` resolves a `bypass` bool
(off when `--ask`/`--no-ask` or the config's `ask` key opts out, flag winning)
and threads it through the flow alongside `agent`. Only Claude's bypass flag
is verified; the others in `BYPASS_FLAGS` are best-guesses.

### Module responsibilities

- `config.py` — resolves `WORKSPACES_DIR` / `WORKTREES_DIR` and the `VV_CONFIG`
  TOML file (all env-overridable; default under `~/.vv/`). Also exposes
  `chats_dir()` (= `WORKTREES_DIR/_chats`) for chat-only sessions. Parses the
  config file (`configured_agent()`, `configured_ask()`, `configured_mode()`,
  `configured_remote()` → the `Remote` dataclass); raises `ConfigError` on
  malformed TOML or a half-configured `[remote]`.
- `agents.py` — `DEFAULT_AGENT`, the `KNOWN_AGENTS` list seeding the picker,
  `PATH` detection (`installed_agents()`, `is_installed()`), and the
  `BYPASS_FLAGS` map + `with_bypass()`.
- `git_ops.py` — `git` CLI wrappers; raises `GitError`.
- `tmux_ops.py` — `tmux` CLI wrappers; raises `TmuxError`.
- `cmux_ops.py` — `cmux` CLI wrappers for remote mode (`is_available()`,
  `new_ssh_workspace()` → opens a `cmux ssh` workspace and returns its id,
  `send_text()` → types into a workspace, `list_workspace_titles()`); raises
  `CmuxError`.
- `remote.py` — remote-launcher orchestration: opens a `cmux ssh` workspace and
  `send`s the `bash -lc '<vv …>'` command into it; `gen_name()` helper.
- `names.py` — curated single-word name list + collision-avoiding picker.
- `cli.py` — Typer app, flow orchestration, interactive menu.

### Conventions to preserve

- All git/tmux/cmux interaction shells out to the CLIs (no library bindings);
  failures surface as `GitError` / `TmuxError` / `CmuxError` (and
  `config.ConfigError` for a bad config file), caught centrally in `cli.main()`.
- The remote vv command is **typed into the remote shell** via `cmux send`, so
  `remote._remote_command()` collapses `[vv, *argv]` into one `shlex.join`'d
  token and wraps it in `bash -lc '<…>'` — both so a URL's `&`/`?` reach the
  remote vv intact and because the `bash -lc` **login** wrapper sources
  `~/.profile` (cmux's interactive remote shell is not guaranteed to be a login
  shell, and `~/.local/bin`, where `uv tool install` puts `vv`, lives there —
  otherwise "command not found"). `launch()` appends a literal `\n` to that
  token: `cmux send` unescapes `\n`/`\r`/`\t`, so it becomes the Enter that
  submits the line. Pass the command as a single token after `send … --` so its
  spaces/quotes aren't re-split. Don't hand-build these strings.
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
