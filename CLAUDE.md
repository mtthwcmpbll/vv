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
  `WORKSPACES_DIR/<repo>` (reused as-is if already present — the fetch belongs
  to `_new_worktree_session()`, see below), then
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
  repo (pick from your GitHub repos via `gh`, or paste a URL), create a brand-new
  GitHub project, or start a chat-only session.

A fifth flow starts no session at all: **`vv --title TEXT`** (`-t`) /
**`vv --label TAG`** (`-l`) → `cli._apply_notes()` annotates an *existing*
session and exits (see "Session notes" below).

A seventh starts no session either: **`vv --watch`** (`-w`) →
`cli._watch_board()` opens the live session board (see "The session board"
below) and returns when you leave it. It is also the first entry in the
interactive menu.

A sixth likewise starts nothing: **`vv --skills`** → `cli._install_skills()`
copies vv's bundled agent skills into every agent tool on this machine and exits
(see "Bundled skills" below).

`_menu_add_repo()` shows a scrollable `questionary.select` of every GitHub repo
the user can access (`_pick_github_repo()`) when `gh_ops.is_available()` (gh on PATH
and logged in). `/` filters the `owner/name` list by **substring** (see
"Filtering a list with `/`" below); `_cap_select_rows()`
limits it to 5 visible rows (it reaches into the prompt_toolkit layout and caps
the choices `Window` height — purely cosmetic, wrapped in a swallow-all `try`).
A first sentinel choice (`_ENTER_URL`) drops to a free-text clone-URL prompt; a
real pick resolves via `gh_ops.clone_url()` using the config's
`clone_protocol` (`config.configured_clone_protocol()`, default `ssh`,
override with `clone_protocol = "https"`). When gh is unavailable the flow is
the original plain URL `questionary.text`. All paths feed `_start_from_url`.

`_menu_new_github_project()` sits directly under "Add a new repo" in the menu and
**creates** the repo before sessioning it, so a new project is one flow instead of
"go to github.com, then come back". It walks four prompts — template → owner →
name → visibility — and then feeds `_start_from_url` exactly like the picker
above, so everything downstream (clone, worktree, tmux, agent) is the existing
path. The template picker (`_pick_template()`) is `_pick_github_repo()`'s twin
(same `/` filter, same `_cap_select_rows(5)`) over
`gh_ops.list_template_repos()`, with an `_EMPTY_REPO` sentinel first so "no
template" stays one keystroke away in a long list. Templates are **filtered from
the same cached `user/repos` walk** as the repo picker (`list_repos_detailed()`
returns `(full_name, is_template)`; `--jq` is applied client-side to gh's cached
response), so opening this menu right after the other costs no extra API calls.
Owners come from `gh_ops.list_owners()` (the user's login first, then their orgs);
the name is validated in the prompt (`_prompt_repo_name()`, GitHub's own
`[A-Za-z0-9._-]` rule) so a typo never becomes a failed round trip. Visibility is
an explicit prompt defaulting to **private**: `gh repo create` has no default in
non-interactive mode, and guessing towards public is the one mistake here that
can't be taken back. Two subtleties: (1) creation is the one gh call that
**raises** (`gh_ops.GhError`) instead of degrading, since falling through to clone
a repo that was never created would be worse than a clear failure; (2) generating
from a template returns *before* GitHub finishes copying the files in, so
`gh_ops.wait_for_commits()` polls until the repo has its first commit — otherwise
the clone lands on an unborn HEAD and vv's empty-repo bootstrap seeds a root commit
that diverges from the content arriving behind it. A poll timeout warns and
continues rather than hanging.

`_new_worktree_session()` picks a random collision-free
`<adjective>-<animal>` name (`names.random_name()`, excluding existing tmux
sessions, git branches, and worktree dirs), creates a worktree on a new branch
of that name off the remote default branch, then calls `_resume_worktree()`. It **fetches first**
(`git_ops.fetch()`), for every create flow rather than in any one of them: a
session cut from a stale `origin/main` starts behind and has to be caught up by
hand later, and the menu's "new session from an existing repo" path in
particular has no other reason to touch the network. The fetch is best-effort —
an unreachable remote is a warning, not a refusal, it just means branching off
the refs already on disk. `git_ops.default_start_ref()` then resolves
`origin/HEAD`, else the checked-out branch's `@{upstream}` (both
*remote-tracking*, so the fetch is what makes them current), else local `HEAD`
— which fetching cannot move, so that last fallback can still be stale.

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
`_delete_session()`) action, plus a **sweep** of all the stale ones at once
(→ `_sweep_stale_sessions()`, see below) and a **`/` filter** over `repo/name`
(see "Filtering a list with `/`" below). Sessions are **ordered by triage state** (`state.ORDER`, most-wants-you-first),
with `_list_worktrees()`'s repo/newest-first order surviving as the stable
tiebreak within a state — so the list reads as a queue rather than a pile. It is
a **loop**: a delete or sweep rebuilds and redraws the list instead of leaving
the menu, so a run of stale
sessions can be cleaned up in one visit (resume and cancel still leave; an
emptied list exits with "No sessions left."). The cursor is carried across the
redraw — `_delete_session()` /
`_delete_chat()` return whether they actually deleted (a declined confirm is
`False`), so the menu focuses the session that slid into the deleted one's slot
(`_next_focus()`: below, else above) or stays on a kept one. Focus reaches the
picker as `_pick_session(focus=…)`, mapped from the `(repo, name)` key to the
live choice value by `_focus_value()` and passed to questionary's `default`
(which sets the initial `pointed_at`); an unmatched key degrades to `None`.
Before rendering, `_session_summaries()` produces
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
a flat row: `_card_lines()` renders a **headline** led by the **triage dot** —
one glyph per `state.py` state (`◆` needs you / `✕` stopped / `▸` working / `◇`
in review / `▹` idle), *not* "is a tmux session up", which was true of every
session that hadn't been deleted and so said nothing. The headline itself is the
user's own `--title` when they set one, else the generated summary. A
title does not *replace* the summary: it displaces it to the row below, indented
and in the quieter `card.summary` style, so a card can carry both. Then, when the
state carries a `detail` (only `needs_you` does), the **agent's own last words**
on one line prefixed by the `detail` glyph — "Should I also update the tests?"
tells you what to do in a way a summary of the work cannot. It is `_fit`-truncated
rather than wrapped, so a chatty agent can't inflate the card. Then — when
the session has any — a row of `#label` chips indented the same way (both from
`notes.all_notes()`; chips are joined by the `label_gap` glyph and wrapped like
the headline, and each row is omitted entirely when empty), then a
`branch [*] · repo/name` line (the branch from `_session_branch()`, *asked of
git* rather than assumed to be the session name — see "The branch is not the
session name" below; the `*`, from `_worktree_dirty()`,
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
`_DEFAULT_COLORS` hold the defaults (the dot's five entries are keyed by the
`state.py` constants themselves — `needs_you` / `stopped` / `working` / `review`
/ `idle` — so `g[session_state.state]` is the whole lookup; the old `running`
key is now `working`), `_card_theme()` layers the config's
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
shows whatever context it could get (or no description at all). Deletion first
asks `_work_at_risk()` (→ `git_ops.is_dirty()` + `git_ops.unpushed_count()`); if
either flags work that would be lost it requires a `questionary.confirm()` before
proceeding. The mechanics then live in `_remove_session()` — kill any live tmux
session, `git_ops.remove_worktree(force=True)` +
`git_ops.delete_branch(force=True)` (so a deleted worktree frees its name for
reuse) — on the branch `_session_branch()` reports, read **before** the worktree
is removed (git can't be run in it afterwards) and falling back to the session
name — `notes.forget()` — deliberately **prompt-free**, because the batch sweep
confirms once for many sessions and must not re-ask per session. Chat sessions
branch through `_delete_chat()` for their warning (no git ops, but the user is
still warned if the directory is non-empty) and `_remove_session()` `rmtree`s
them. Every deletion path (plus `_delete_repo()`, via `notes.forget_repo()`)
clears the session's notes so they don't linger in the store.

#### Filtering a list with `/` (`_enable_filter()`)

Every list long enough to hunt through — the session cards, the cloned-repo
picker, and the gh repo/template/owner pickers — filters the same way: **`/`**
starts it, typing narrows the list to choices whose title contains the text,
backspace rubs it out, and **Esc** leaves and clears it. Arrow keys and Enter
keep working while typing, so a filter can be typed and its result resumed
without leaving the mode. Matching is questionary's own (`control.search_filter`
+ `filtered_choices`, a case-insensitive substring of the choice *title*), which
for sessions is `repo/name` — the worktree name and its repo, not the summary,
labels or branch on the card. A filter matching nothing falls back to showing
everything (questionary's behavior), and the `/ text…` footer under the list is
likewise questionary's, drawn for any select whose `search_filter` is set — vv
only ever sets it.

Filtering is **modal** for one reason: questionary's own `use_search_filter`
binds *every* printable key unconditionally, which cannot coexist with the
single-key shortcuts — `x` in the middle of a filter would delete the
highlighted session. So `_enable_filter()` binds the characters itself, behind a
prompt_toolkit `Condition` it returns; callers guard their own bindings with
`filter=~typing` so `x`/`X` become characters while a filter is being typed
(and, being inactive, lose to the filter's binding for the same key regardless
of registration order). Two details: the cursor **keeps its choice** across
every filter change when that choice is still visible (so Esc leaves you where
you were looking, not at the top); and `Esc` is bound *non-eagerly* while
`Escape Enter` — which prompt_toolkit hands to its empty prompt buffer, and
which would otherwise answer `""` — is bound explicitly to "leave the filter and
select", since leaving and confirming in one motion arrives as a single meta
sequence.

Because the cards are rendered by vv rather than questionary, `_pick_session()`
must narrow them itself: `_visible_cards()` maps `control.filtered_choices` back
to cards, since `pointed_at` indexes the *filtered* list and the highlight would
otherwise land on the wrong session.

#### Bulk cleanup of stale sessions (Shift+X)

Sessions accumulate faster than they get deleted, so the session list binds
**`X`** (Shift+X, next to `x`-deletes-one) to `_sweep_stale_sessions()`: it
proposes every stale session, prints the list to **smoke-test first**, and
deletes the lot behind a single `questionary.confirm()`. Nothing is touched
before that confirm. The pointed-at session rides along with the `_SWEEP`
sentinel so the cursor can be restored after the redraw.

`_classify_stale()` owns the definition, and errs towards keeping things —
a wrongly-swept session is unrecoverable, a wrongly-kept one costs a keystroke.
Two reasons qualify: the session's **PR is merged** (the work landed;
`_REASON_MERGED`), or it has **no local changes** (`_REASON_UNTOUCHED`: clean
tree, nothing unpushed, *and* `git_ops.commits_ahead()` = 0 against the repo's
`default_start_ref()` — so the branch never diverged from where it was cut).
`commits_ahead` is why a pushed branch with an open PR is not swept for looking
"clean": `unpushed_count()` alone can't tell "nothing was ever done here" from
"the work is pushed and under review". A **dirty working tree vetoes both
reasons**: uncommitted changes exist nowhere else, so a merged PR doesn't make
them safe to bin — such a session is only ever deleted one at a time via
`_delete_session()`, whose per-session warning names what is being lost. Five
deliberate exclusions, then: chat sessions (no branch and no PR to judge them
by), a session with **uncommitted changes** (whatever its PR says), a **running**
session for the untouched reason (that's the one you just opened — merged-PR
sessions are still offered, with "session is running" listed as a cost), a
session whose git state can't be read (skipped rather than assumed empty), and
any open/draft/closed PR with commits behind it.
A merged-PR session still reports its `_work_at_risk()` in the listing — merging
says nothing about commits that never got pushed — so the confirmation is
informed rather than blind.

Two details worth keeping: (1) `_resolve_pr_status()` **blocks** on `gh` for any
session the PR cache doesn't know (the cards' background refresh may not have
landed, and treating an unknown PR as "not merged" would silently under-clean) —
the one place in vv where waiting on `gh` is the right trade, and it prints
"Checking N session(s)…" while it does; (2) removals are individually wrapped, so
one locked worktree reports `! kept repo/name: …` and the rest of the batch still
goes through — the closing tally is "Deleted N of M".

### Triage state: whose turn is it (`state.py`)

The cards always answered *what* a session is — summary, branch, PR. The
question you actually ask with twenty open is **which one is waiting on me**,
and nothing answered it: the old running dot only meant "a tmux session exists",
which is true of everything you haven't deleted.

`state.py` computes that missing axis. Five states in `ORDER`, most-wants-you
first: `needs_you`, `stopped`, `working`, `review`, `idle`. It drives the card
dot, the session list's ordering, and the board's lanes.

Three signals, **all already paid for elsewhere**, which is what makes this cheap
enough to recompute on a timer:

- `tmux_ops.session_activity()` — one `list-panes -a` for the whole server
  (so fifty sessions cost what two do), giving each session's `session_activity`
  timestamp and its **active pane's foreground command**. That second field is
  the one nothing else had: a pane back at a shell (`Activity.at_shell`, against
  `tmux_ops.SHELL_COMMANDS`) means the agent vv launched has exited, which is
  otherwise invisible — such a session looks healthy from the outside.
- `summary.last_turn()` — the last *real* conversation turn, reusing the same
  transcript readers and `_clean_turn` filter the summaries use (so "the agent
  spoke last" means it actually said something, not that it made a tool call).
  Memoized on the transcript's mtime, so a refresh is free for every session that
  hasn't moved.
- the `dirty` / `pr` facts the cards compute anyway, passed in as `state.Facts`
  rather than recomputed.

`classify()` is **pure and fully injectable** — every input is an argument — so
the whole machine is testable without tmux, a transcript or a repo. The rules,
and why:

- **No tmux session** → nothing is running, so nothing is waiting on you: the
  work decides (`review` if a PR is open/draft, else `idle`). Note that
  uncommitted work here is deliberately *not* `needs_you`: it is already flagged
  by the card's dirty marker and protected by the sweep, and counting it as
  urgent would flood the lane with every parked session carrying a stray edit.
- **Pane at a shell, and quiet** → `stopped`. The quiet requirement is the guard
  against a false positive: an agent shelling out for a tool call can briefly put
  a shell in the foreground while output is still flowing.
- **Output within `Windows.active`** (60s) → `working`.
- **Quiet, agent spoke last** → `needs_you`, carrying the agent's message as
  `detail`.
- **Quiet, you spoke last** → still `working`; the agent hasn't answered yet, it
  is just slower than the active window.
- **Quiet past `Windows.stale`** (24h) → falls out of `needs_you` into
  `review`/`idle`.

That last rule is the load-bearing one, and the reason the lane is useful. An
agent that stopped an hour ago is waiting on you; one that stopped last Tuesday
is abandoned, and treating the two the same is precisely how the pile became
indistinguishable. `needs_you` therefore holds *today's* work, and everything
older drains into `idle` where `_sweep_stale_sessions` can eat it. Both windows
come from the config's `[board]` table via `cli._state_windows()`.

`classify_all()` is the batch entry point: one tmux call, transcript reads fanned
over a small pool, keyed exactly like the sessions dict it is given. Missing tmux
degrades to "nothing is running" rather than raising — states are UI decoration,
and a board that dies because tmux isn't installed is worse than one that shows
everything idle.

### The session board (`vv --watch`)

The session list is something you open when you remember to; the board is
something you leave open in a cmux tab and glance at. `_watch_board()` renders
every session as a card in a **lane per triage state**, and re-reads the world
every `[board] refresh` seconds.

`_BoardModel.reload()` rebuilds wholesale, which is only affordable because
nothing in it runs an agent or blocks on the network: states as above, PR
statuses from the `pr.Snapshot` cache with the stale ones refetched in a
background thread and picked up by the *next* reload (one at a time, so a slow
`gh` can't stack threads on a board left open all day), and summaries read
straight from `summary.load_cache()` and **never regenerated** — `summarize_all`
on a timer would spawn an agent process per session, per tick. There is a test
that fails if the board ever calls it.

Lanes are dropped when empty, **except `needs_you`**, which is kept even at zero:
"nothing is waiting on you" is the most useful thing the board can say, and it
can only say it by leaving the lane visible.

`_board_rows()` lays lanes out **side by side when they fit** (`len(lanes) *
_LANE_MIN_WIDTH`) and stacks them vertically when they don't, so a narrow
terminal gets one tall readable column instead of four cramped ones. Every row it
emits is padded to exactly the same width — including the blank separators —
because in column mode a single over-long row shears every column to its right.
That invariant is why `_card_lines`' `content_row()` now `_fit_segments()`-truncates:
a long `branch · repo/name` could always overrun its card, it just clipped
harmlessly at the terminal edge in the single-column list. Lane headers and rules
are sized by `_card_row_width()`, mirroring `_card_lines`' own `_CARD_MAX_WIDTH`
clamp, so on a wide screen the rule stops where the cards stop instead of
floating past them.

`_run_board()` is a full-screen prompt_toolkit `Application` (not questionary —
there is no list to pick from). A daemon thread reloads and `invalidate()`s;
`↑↓` move, `←→` jump lanes, `enter` resumes, `x` deletes, `r` refreshes, `q`
quits. Two details: the cursor is remembered **by session folder, not by index**,
so a reload that re-lanes a session (it finished; it started asking) keeps the
cursor on it rather than dumping it wherever that slot now points; and actions
are *returned* rather than performed, because both resuming and deleting need the
terminal back — resume hands it to tmux, delete prompts via questionary. A delete
loops back into a rebuilt board, like the list menu.

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

### Bundled skills (`vv --skills`)

vv ships agent skills under `vv/_skills/<name>/SKILL.md` — currently just `pr`,
which commits, renames the branch to `<type>/<short-summary>`, pushes, and opens
the PR. A skill is only useful once the agent can *see* it, and every agent looks
somewhere different, so `vv --skills` installs them.

The happy accident that makes this a copy rather than a translation: all five
supported tools read personal skills from `<root>/skills/<name>/SKILL.md`
(Anthropic's Agent Skills layout). Only the root differs, which is all
`skills._ROOTS` records — Claude Code `~/.claude` (or `$CLAUDE_CONFIG_DIR`),
Cursor `~/.cursor`, GitHub Copilot `~/.copilot`, Codex `~/.codex` (or
`$CODEX_HOME`), and Antigravity **`~/.gemini/config`** — that last one is the
non-obvious one: it is the only global location all three Antigravity flavors
(IDE, CLI, agy) agree on, and its `~/.gemini/antigravity/` siblings are
conversation state, not configuration. Roots resolve in `skills.targets()` at
call time, not import, so `$HOME`/env changes (and tests) are seen.

A tool counts as **found** when its root directory exists (`skills.discovered()`).
vv creates the `skills/` directory under a found root but never the root itself:
inventing `~/.codex` for someone who has never installed Codex would leave a
stray config dir that tool would then have opinions about. Tools that aren't
found are listed as skipped, not treated as an error.

`skills.status()` is the three-way the flow turns on — `missing`, `same`, or
`differs` — computed from a `_digest()` of the whole skill tree, not just
`SKILL.md`, since skills may carry `scripts/` and `references/`. `differs` can't
distinguish "stale copy from an older vv" from "the user edited it", so
`_install_skills()` never silently overwrites: it lists every conflict and asks
**once** (the stale sweep's "show the batch, confirm once" shape). Declining
keeps those and still installs everywhere the skill is missing. `install()`
`rmtree`s before copying rather than merging, so a file the skill has since
dropped can't linger and keep the tree reading `differs`. Individual installs are
wrapped, so an unwritable root reports `!` and the batch continues.

Two placement details: the data dir is `vv/_skills/` and **not** `vv/skills/`,
which would sit next to `vv/skills.py` and resolve only by the
namespace-package tiebreak; and `--skills` is handled before mode resolution in
`cli.main()`, so it never routes through remote mode — it configures *this*
machine's tools, and inside a remote session you are already running the remote
vv against the tools you actually want the skills in.

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
`vv <url>` / `vv --chat` run the remote create flow, and `vv --watch` forwards
too (tab titled `board`) — the board belongs on the machine whose sessions it
shows, and opening a local one over an empty machine would be useless. Note this
is the *opposite* call from `--skills` and `--title`/`--label`, which stay local:
those configure or annotate *this* machine, the board reads the *sessions*. `--local` is always
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
  session-card theme), `configured_board()` → the `Board` dataclass (the
  `[board]` table: `refresh` for the board's timer, `active_window` /
  `stale_after` for the two `state.Windows`, so retuning them also retunes every
  card's dot), `configured_ask()`, `configured_mode()`,
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
- `state.py` — the triage state of each session: whose turn is it (see "Triage
  state" above). Owns the five state constants, `ORDER` / `LABELS` / `RANK`, the
  `Windows` (active/stale) that separate them, `Facts` (the caller-supplied git
  and PR facts), the `SessionState` result, the pure `classify()` and the batch
  `classify_all()`. Depends only on `tmux_ops` and `summary`; runs no agent,
  touches no network, and never raises.
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
  `last_turn()` is the other public reader — the last real `(role, text)` turn,
  memoized on the transcript's mtime — which is how `state.py` knows whether the
  agent or the user spoke last.
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
  repo" picker and the "Create a new GitHub project" flow: `is_available()` (on
  PATH **and** authenticated), `list_repos_detailed()` (every
  `(owner/name, is_template)` the user can access via the `user/repos` API,
  paginated and `gh`-cached for an hour — spans org repos, not just the user's
  own) with `list_repos()` / `list_template_repos()` as views over that one
  cached walk, `list_owners()` (login + orgs, for "who owns the new repo"),
  `create_repo()` (`gh repo create`, optionally `--template`), `has_commits()` /
  `wait_for_commits()` (poll a freshly generated repo until GitHub finishes
  copying the template in), `clone_url()` (maps a picked `owner/name` to a
  github.com URL in the caller-supplied protocol — SSH `git@github.com:…` by
  default, else HTTPS; resolved from `config.configured_clone_protocol()`), and
  `pr_status()` (runs `gh pr view` in a worktree → normalized
  `{number, state, checks}` for the session card, or `None`). The *discovery*
  helpers **never raise** — every failure degrades to `[]`/`None` so the menu
  falls back gracefully; `create_repo()` is the one deliberate exception
  (`GhError`, caught in `cli.main()` with the other ops errors) because it
  mutates GitHub and there is nothing to fall back to.
  Note: `gh api` silently switches to **POST** as soon as any `-f` parameter is
  present, so every read that passes one must also pass `-X GET` or it 404s.
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
- `tmux_ops.py` — `tmux` CLI wrappers; raises `TmuxError`. Beyond session
  lifecycle it exposes `session_activity()` → `{name: Activity}`, the single
  whole-server `list-panes -a` that feeds `state.py` (last-activity timestamp,
  attached, the active pane's foreground command, and `pane_dead`), plus
  `SHELL_COMMANDS` and `Activity.at_shell` for "the agent exited".
- `cmux_ops.py` — `cmux` CLI wrappers for remote mode (`is_available()`,
  `new_ssh_workspace()` → opens a `cmux ssh` workspace and returns its id,
  `send_text()` → types into a workspace, `list_workspace_titles()`); raises
  `CmuxError`.
- `remote.py` — remote-launcher orchestration: opens a `cmux ssh` workspace and
  `send`s the `bash -lc '<vv …>'` command into it; `gen_name()` helper.
- `skills.py` — installs the skills bundled in `vv/_skills/` into the agent tools
  on this machine (see "Bundled skills"). `targets()` resolves every supported
  tool's root, `discovered()` filters to the ones present, `bundled_skills()`
  lists what vv ships, `status()` compares a skill's installed tree against the
  bundled one, and `install()` copies it in. Pure filesystem work — it shells out
  to nothing and knows nothing about the CLI's prompting.
- `names.py` — two curated word pools (`ADJECTIVES` positive adjectives,
  `ANIMALS`) combined into `<adjective>-<animal>` session names, plus the
  collision-avoiding picker. `random_name()` draws a random pair (retrying a few
  times), only enumerating `all_names()` — the full ~42k product — if the draws
  keep colliding, and suffixes a number in the impossible case that every
  combination is taken.
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
- The worktree name seeds the branch name *and* is the tmux session name — keep
  `names.ADJECTIVES` / `names.ANIMALS` entries valid as both (no `.`, `:`, `/`,
  or spaces) and free of `-`, which separates the two halves.
- **The branch is not the session name.** A session starts on a branch named
  after its worktree, but nothing holds it there: a PR flow may `git branch -m`
  it to something readable (`feat/session-cards`) so reviewers see intent rather
  than `brave-falcon`. Anything that *shows* or *deletes* a session's branch must
  therefore ask git — `cli._session_branch()`, which is best-effort and returns
  `None` on a git error or a detached HEAD so callers can fall back to the
  session name. The tmux session and worktree directory keep the original name
  (a branch name may contain `/`, those may not), so the two legitimately
  diverge. `_list_worktrees()`, name-collision avoidance, and the notes/summary/PR
  cache keys are all keyed on the *session* name and are unaffected.
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
