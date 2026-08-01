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
  `_new_worktree_session()`. A brand-new remote with no commits clones to an
  unborn HEAD (nothing to branch from), so when `git_ops.has_head_commit()` is
  false the default branch is first bootstrapped with an empty root commit
  (`git_ops.seed_initial_commit()`) and pushed (`git_ops.push_current()`,
  best-effort — a warning, not fatal, if the remote is unreachable). Worktrees
  then branch off `main` as usual instead of a disposable worktree branch
  becoming the repo's first branch.
- **`vv --chat`** (a.k.a. `-c`) → `cli._new_chat_session()`: create an empty
  directory under `WORKTREES_DIR/_chats/<name>` (no git involved), then
  `_resume_worktree()`. For persistent agent conversations that don't need
  version control. Cannot be combined with a repo URL.
- **`vv`** (no args) → `cli._interactive_menu()`: a `questionary` menu to
  list existing sessions, start a worktree from an already-cloned repo, add a
  repo (pick from your GitHub repos via `gh`, or paste a URL), or start a
  chat-only session.

A fifth flow starts no session at all: **`vv --title TEXT`** (`-t`) /
**`vv --label TAG`** (`-l`) → `cli._apply_notes()` annotates an *existing*
session and exits (see "Session notes" below).

`_menu_add_repo()` shows a scrollable `questionary.select` of every GitHub repo
the user can access (`_pick_github_repo()`) when `gh_ops.is_available()` (gh on PATH
and logged in). Typing filters the `owner/name` list by **substring**
(`use_search_filter=True`, which forces `use_jk_keys=False`); `_cap_select_rows()`
limits it to 5 visible rows (it reaches into the prompt_toolkit layout and caps
the choices `Window` height — purely cosmetic, wrapped in a swallow-all `try`).
A first sentinel choice (`_ENTER_URL`) drops to a free-text clone-URL prompt; a
real pick resolves via `gh_ops.clone_url()` using the config's
`clone_protocol` (`config.configured_clone_protocol()`, default `ssh`,
override with `clone_protocol = "https"`). When gh is unavailable the flow is
the original plain URL `questionary.text`. All paths feed `_start_from_url`.

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

The "start a new session from an existing repo" menu (`_menu_new_from_repo()`)
lists cloned repos via `_pick_repo()`, which also binds **`x`** on the
highlighted repo to delete it wholesale (→ `_delete_repo()`): it confirms,
listing any worktrees that would be lost (flagged when running / dirty /
unpushed), then kills their live tmux sessions and `shutil.rmtree`s both the
per-repo worktrees dir and the workspace clone. (`_pick_repo()` reaches into
questionary's prompt_toolkit `Application` to add the key — `select` exposes no
public hook — and returns a `("select" | "delete" | "cancel", repo)` tuple.)

The "list existing sessions" menu (`_menu_list_sessions()`) offers each chosen
worktree a **resume** (→ `_resume_session()`) or **delete** (→
`_delete_session()`) action. Before rendering, `_session_summaries()` produces
a one-line description of what each session is working on. Summaries are
**cached** on disk (`config.summary_cache_file()` → `WORKTREES_DIR/.summaries.json`)
keyed by a cheap *activity fingerprint* (`summary.session_fingerprint()`: the
session dir's mtime + the driving transcript file's mtime), so on a menu open
only sessions that have actually changed since last time — been opened and worked
in — are regenerated; the rest are served from cache. The cache is rewritten to
exactly the current sessions each open (pruning deleted ones), and entries also
carry the `agent` they were generated with so switching `summary_agent`
invalidates them.

Each session is drawn as a **cmux-style card** (a bordered rectangle) rather than
a flat row: `_card_lines()` renders a **headline** (with a `●` running / `○` idle
dot) — the user's own `--title` when they set one, else the generated summary. A
title does not *replace* the summary: it displaces it to the row below, indented
and in the quieter `card.summary` style, so a card can carry both. Then — when
the session has any — a row of `#label` chips indented the same way (both from
`notes.all_notes()`; chips are joined by the `label_gap` glyph and wrapped like
the headline, and each row is omitted entirely when empty), then a
`branch [*] · repo/name` line (the `*`, from `_worktree_dirty()`,
flags uncommitted/unpushed work; no leading glyph — an uncommon symbol like `⎇`
renders wide on some phone fonts and clips the branch name), then a color-coded
PR-status line with a right-aligned relative timestamp (`_relative_time()`). Cards
also leave `_CARD_RIGHT_MARGIN` + `_CARD_TEXT_SLACK` of slack (outside the card,
and before the right-aligned timestamp) so a glyph a terminal renders wider than
measured can't clip content. `_pr_segment()` encodes PR **state** with a default
plain-Unicode circle family (`◌` draft / `○` open / `●` merged) plus `⊘` closed;
for an open PR the check rollup adds a `✓`/`✗`/`◔` mark and drives the color
(green/red/yellow), else cyan for a checkless open PR. Non-git sessions show
`❝ chat session`, git sessions with no PR `○ no open PR`. All glyphs are themeable
(see below) and default to plain Unicode (not Nerd Font octicons) so they render
on a mobile terminal without the patched font. Draft state comes from `gh`'s
`isDraft` (see `gh_ops.pr_status`). `_pick_session()`
reuses questionary's `select` (navigation, Enter, and the `x`-to-delete binding)
but **swaps out the per-choice renderer**: it sets `control.text` to
`_render_cards()`, which prompt_toolkit re-invokes each keystroke, so the card at
`control.pointed_at` gets a left selection bar (`▌`) and a `card.sel` background
wash layered onto every segment — full color *and* whole-card highlight, which
questionary's built-in string/`class:highlighted` rendering can't do together.
Every card **glyph and color is themeable from config**: `_DEFAULT_GLYPHS` /
`_DEFAULT_COLORS` hold the defaults, `_card_theme()` layers the config's
`[cards.glyphs]` / `[cards.colors]` tables (`config.configured_card_glyphs()` /
`configured_card_colors()`) over them into a `CardTheme` that threads through
`_card_lines` / `_pr_segment` / `_render_cards` / `_card_style` (each defaults to
`_DEFAULT_THEME` so tests and callers can omit it). Colors are prompt_toolkit
style strings; `_valid_colors()` probes each override and drops any prompt_toolkit
can't parse, so a config typo can't crash the menu at render. Styling is a
`_card_style()` prompt_toolkit `Style` passed to `select`. PR status
loads **non-blocking**: `pr.Snapshot` serves whatever is cached instantly (a
stale git session shows `⋯ checking…`), and `_pick_session` kicks off its
background `refresh()` while the menu runs — as each session's live status lands,
the matching card's `pr` is updated and `app.invalidate()` repaints (thread-safe),
so the view enriches after a beat without ever blocking input. A `threading.Event`
stops the callbacks the moment the user leaves the view. Summaries come from
`summary.summarize_all()`, which runs the
configured **summary agent** (`config.configured_summary_agent()`, falling back
to the session agent) in non-interactive "print" mode over each session in
parallel. The context it feeds that agent (`summary._gather_context()`) blends
**two sources** so a session is summarizable even with nothing committed: the
git state (branch + session commits + uncommitted changes/diff, or a chat's file
listing) **and** the recent agent conversation pulled from the session's
transcript. The transcript is located by probing each known agent's store in
order — **Claude, then Gemini, then Codex** — and using the first that has a
conversation for this session (later agents aren't checked), since vv doesn't
record which agent ran a session. It is entirely best-effort: if the summary
agent has no known print-mode invocation (`summary.PRINT_FLAGS`, only `claude`'s
is verified), no store has a transcript, or every summary fails, the menu just
shows whatever context it could get (or no description at all). Deletion first checks `git_ops.is_dirty()` and
`git_ops.unpushed_count()`; if either flags work that would be lost it requires
a `questionary.confirm()` before proceeding. It then kills any live tmux
session and runs `git_ops.remove_worktree(force=True)` +
`git_ops.delete_branch(force=True)` — so a deleted worktree frees its name for
reuse. Chat sessions branch through `_delete_chat()` instead: no git ops, but
the user is still warned if the directory is non-empty before `shutil.rmtree`.
Both deletions (and `_delete_repo()`, via `notes.forget_repo()`) call
`notes.forget()` so a deleted session's notes don't linger in the store.

### Session notes (title + labels)

The two **manual** levers for telling many sessions apart, deliberately kept in
one store and one flow because they behave identically:

- `vv --title TEXT` / `-t TEXT` sets a one-line title the user writes; a blank
  title (`vv -t ""`) clears it.
- `vv --label TAG` / `-l TAG` attaches a free-text label; `--label=-TAG` removes
  it. The flag is **repeatable** and specs are applied in order.

Both take the same two shapes, and can be combined in one invocation:

- **On their own** (`vv -t "Acme" -l urgent`) → `cli._apply_notes()` annotates an
  existing session and exits without starting anything. The target is
  `--name NAME` if given, else the session the **cwd** is inside
  (`_session_from_cwd()` matches the cwd or any subdirectory of it against
  `_list_worktrees()`) — so inside a session you can just annotate it. Neither
  resolving? A hard error, since silently annotating the wrong session would be
  worse.
- **Alongside a create flow** (`vv <url> -t "Acme"`, `vv --chat -l acme`) → the
  flags are bundled into a `notes.Pending` that threads through
  `_start_from_url` / `_new_worktree_session` / `_new_chat_session` as
  `pending_notes` and is stamped by `_note_new_session()` once the name is
  settled but *before* `_resume_worktree()` hands over the terminal (nothing
  runs after the attach). A bad label spec there is a warning, not a failure —
  the session already exists.

Notes are **user data, not a cache**: `notes.py` owns a single JSON store
(`config.notes_file()` → `WORKTREES_DIR/.session-notes.json`, same
version-stamped shape and `"<repo>/<name>"` keys as the summary/PR caches) that
nothing regenerates. Each entry is a `Note(title, labels)`; `set_title()` and
`apply_labels()` each rewrite only their own half, so the two levers never
clobber each other. `apply_labels()` parses every spec up front (`parse_spec()`,
which rejects a bare sign) so a typo in the last spec doesn't half-apply the
rest; matching is case-insensitive (no `Acme`/`acme` duplicates) while the
casing the user typed is preserved, order is insertion order, and re-adding or
removing a missing label is a no-op rather than an error. A title is collapsed
to a single line (`clean_title()`) so a pasted paragraph can't wreck the card
layout. Sessions left with neither a title nor labels are dropped from the store
on write, and a falsy `Note` is how "nothing set" is tested throughout.

In **remote mode** the two shapes diverge deliberately: a create flow forwards
its flags (as `--title=<text>` / `--label=<spec>`, the `=` form so a removal's
leading `-` — or a title starting with one — can't be read as a flag by the
remote's parser), but annotating an existing session is handled *locally and
never launches a cmux tab* — inside a remote session you are already running the
remote's own vv, whose config has no `[remote]`.

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
  `chats_dir()` (= `WORKTREES_DIR/_chats`) for chat-only sessions,
  `summary_cache_file()` (= `WORKTREES_DIR/.summaries.json`) for the summary
  cache, `pr_cache_file()` (= `WORKTREES_DIR/.pr-status.json`) for the PR cache,
  and `notes_file()` (= `WORKTREES_DIR/.session-notes.json`) for the user's
  session titles/labels. Parses the
  config file (`configured_agent()`, `configured_summary_agent()`,
  `configured_card_glyphs()` / `configured_card_colors()` (the `[cards.*]`
  session-card theme), `configured_ask()`, `configured_mode()`,
  `configured_clone_protocol()` → `ssh`/`https`, `configured_remote()` → the
  `Remote` dataclass); raises `ConfigError` on malformed TOML or a
  half-configured `[remote]`.
- `agents.py` — `DEFAULT_AGENT`, the `KNOWN_AGENTS` list seeding the picker,
  `PATH` detection (`installed_agents()`, `is_installed()`), and the
  `BYPASS_FLAGS` map + `with_bypass()`.
- `notes.py` — the user-set session title and labels shown on the cards (see
  "Session notes"). Owns the JSON store (`load()` / `save()` / `all_notes()` /
  `for_session()` → a `Note`), the two writers (`set_title()`,
  `apply_labels()` → `(labels, added, removed)`), their input normalizers
  (`clean_title()`; `parse_spec()`, raising `LabelError`), the `Pending` bundle
  the CLI threads into create flows, and cleanup on deletion (`forget()` /
  `forget_repo()`). Reads degrade to `{}` on a corrupt or version-mismatched
  store and skip malformed entries; writes are atomic and best-effort.
- `summary.py` — generates the one-line session summaries shown in the "list
  existing sessions" menu. `PRINT_FLAGS` maps an agent command to the tokens
  that run it non-interactively (only `claude`'s is verified); `summarize()`
  feeds a session's context to that agent and keeps the first printed line;
  `summarize_all()` fans out over sessions with a thread pool. Context comes
  from both git (`_git_context`/`_dir_context`) and the session's agent
  transcript (`_transcript_context`), which probes each agent's store in order
  (`_claude_messages` → `_gemini_messages` → `_codex_messages`) and renders the
  first that yields turns (`_render_messages` keeps the first + last few *real*
  user/assistant turns; tool calls and slash/bash-command turns filtered out via
  `_clean_turn`). Each store is located differently — Claude by encoding the cwd
  into a `~/.claude/projects/<encoded>` dir name (`_encode_project_path`); Gemini
  by mapping the cwd through `~/.gemini/projects.json` to a tag dir under
  `~/.gemini/tmp/<tag>/chats`; Codex by scanning `~/.codex/sessions` newest-first
  for a `rollout-*.jsonl` whose header `cwd` matches. All the store locations are
  module constants (overridable in tests). Best-effort throughout — never raises,
  returns `None`/`{}`/`""` on any failure. Also owns the summary **cache**
  (`session_fingerprint()`, `load_cache()`, `save_cache()`); the fingerprint
  reuses the same per-agent transcript file-finders (`_claude_file` /
  `_gemini_file` / `_codex_file`, factored out of the message providers) via
  `_transcript_path()` so it reflects the file that actually drives the summary.
  Two subtleties keep the cache and summaries honest: (1) `summarize()` runs the
  agent in an isolated scratch dir (`_scratch_cwd()` → `WORKTREES_DIR/.summary-scratch`),
  never the session — agent CLIs persist a transcript for their cwd, so running
  in-session would pollute the very history we read back and bump the fingerprint
  on every run (the context is all passed in the prompt, so no session access is
  needed); (2) `_claude_file()` skips transcripts that are vv's *own* summary
  runs (`_is_summary_run()`, detected by the `_SUMMARY_MARKER` opening of
  `_PROMPT`), so a summary never feeds on a previous summary.
- `git_ops.py` — `git` CLI wrappers; raises `GitError`.
- `gh_ops.py` — optional `gh` (GitHub CLI) wrappers powering the "Add a new
  repo" picker: `is_available()` (on PATH **and** authenticated),
  `list_repos()` (every `owner/name` the user can access via the `user/repos`
  API, paginated and `gh`-cached for an hour — spans org repos, not just the
  user's own), and `clone_url()` (maps a picked `owner/name` to a github.com
  URL in the caller-supplied protocol — SSH `git@github.com:…` by default, else
  HTTPS; resolved from `config.configured_clone_protocol()`), and `pr_status()`
  (runs `gh pr view` in a worktree → normalized `{number, state, checks}` for the
  session card, or `None`). Unlike the other ops modules it **never raises** —
  every failure degrades to `[]`/`None` so the menu falls back gracefully.
- `pr.py` — cached, background-refreshed pull-request status for the session
  cards. `Snapshot(sessions)` exposes `.cached` (PR statuses already known, no
  `gh` calls) and `.stale_keys` (sessions to refetch); `.refresh(on_result, stop)`
  spawns a daemon thread that fetches the stale ones (`gh_ops.pr_status`) in
  parallel, calls `on_result(key, pr)` as each lands, and rewrites the cache.
  Cache is keyed by a `session_fingerprint()` of branch + HEAD commit, so a
  session is only refetched once its branch moves (CI checks that finish without
  a new commit lag until the next push — the deliberate trade for a fast menu).
  Same on-disk cache shape as `summary` (version-stamped JSON, pruned to current
  sessions each refresh).
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
  blocking `subprocess.run`. In the `execvp` branch it first emits an **OSC 7**
  sequence (`_report_cwd`) reporting the worktree to the enclosing terminal, so
  cmux/Ghostty (and iTerm2/WezTerm/kitty) show the worktree as the tab's
  directory instead of wherever vv was launched: tmux consumes the agent's own
  OSC 7 rather than forwarding it, and no outer shell prompt fires again once
  tmux takes over, so without this one final OSC 7 the terminal stays frozen on
  the launch directory. Guarded by `sys.stdout.isatty()`.
- That one-shot OSC 7 goes stale on any *re*-attach (cmux/SSH reconnect,
  detach-and-reselect) where vv isn't in the loop to re-send it. So
  `create_session()` also calls `_setup_cwd_forwarding()`: it turns on the
  session's `allow-passthrough` option and installs a `client-attached` tmux hook
  that re-reports the worktree on every attach. Since tmux *swallows* a pane's
  OSC 7 rather than relaying it, the hook can't just print OSC 7 — it runs
  `vv --emit-cwd <worktree>` with stdout redirected to the attaching client's
  `#{pane_tty}`, and `emit_cwd()` prints the OSC 7 wrapped in tmux's DCS
  **passthrough** (`\ePtmux;<payload-with-ESC-doubled>\e\\`), which tmux unwraps
  and forwards to the outer terminal. We bake the literal worktree path into the
  hook, not `#{pane_current_path}`: it's the dir whose branch/PR cmux should show
  and it avoids forwarding the transient cwd a shell reports mid-rc-file-sourcing
  when a client attaches during startup. The hook shells out to the absolute vv
  path (`_self_command()`, resolved via the current PATH); double-quote the vv
  path and worktree for `/bin/sh` and keep the whole `run-shell` argument
  single-quote-free so tmux's own single-quoting holds. All of it is best-effort
  (`check=False`): a pre-3.3 tmux without `allow-passthrough`, or a vv not on
  PATH, just means no live re-sync. Requires **tmux ≥ 3.3**.
- vv-created tmux sessions are stamped with the `@vv` session option
  (`tmux_ops.VV_TAG`); `list_sessions(vv_only=True)` filters on it. The
  unfiltered `list_sessions()` feeds collision avoidance, which must consider
  *all* tmux sessions, and the "running" annotation in the resume menu.
