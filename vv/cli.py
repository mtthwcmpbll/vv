"""Command-line entry point for vv."""

from __future__ import annotations

import re
import shutil
import textwrap
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import questionary
import typer

from . import (
    agents,
    cmux_ops,
    config,
    gh_ops,
    git_ops,
    names,
    notes,
    pr,
    remote,
    summary,
    tmux_ops,
)

app = typer.Typer(
    add_completion=False,
    help="Spin up disposable git worktree + tmux + agent CLI sessions.",
)

# Sentinel "repo" identifier for chat-only sessions: they live under
# WORKTREES_DIR/_chats/<name> instead of belonging to a real cloned repo.
CHATS = "_chats"

# Sentinel returned by the repo picker when the user pressed the delete
# shortcut instead of selecting a repo to start a session from.
_DELETE = object()

# Sentinel returned by the session picker when the user pressed the bulk-cleanup
# shortcut (Shift+X) instead of picking a single session to act on.
_SWEEP = object()


def _fail(message: str) -> "typer.Exit":
    """Print an error and return an Exit to raise."""
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(code=1)


def _list_repos() -> list[str]:
    """Return repo names that have been cloned into the workspaces dir."""
    root = config.workspaces_dir()
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def _created_ts(path: Path) -> float:
    """Return the session's creation time as a Unix timestamp.

    Uses the worktree/chat directory's birth time where the platform exposes it
    (macOS ``st_birthtime``), falling back to the mtime otherwise. Returns
    ``0.0`` if the path can't be stat'd, so ordering never crashes on a session
    that vanished underfoot.
    """
    try:
        st = path.stat()
    except OSError:
        return 0.0
    return getattr(st, "st_birthtime", None) or st.st_mtime


def _format_created(ts: float) -> str:
    """Format a creation timestamp as e.g. ``07/15/2026 1:34pm``."""
    dt = datetime.fromtimestamp(ts)
    hour = dt.hour % 12 or 12
    return f"{dt:%m/%d/%Y} {hour}:{dt:%M}{'am' if dt.hour < 12 else 'pm'}"


def _list_worktrees() -> list[tuple[str, str, Path]]:
    """Return ``(repo, name, path)`` for every vv session across all repos.

    Chat-only sessions (no git worktree) are surfaced under the sentinel
    :data:`CHATS` namespace so the same menu can resume / delete them.

    Ordered by repo, then most-recently-created first within each repo, so the
    newest sessions for a repo (usually what you just spun up) sort to the top.
    """
    worktrees_root = config.worktrees_dir()
    found: list[tuple[str, str, Path]] = []
    for repo in _list_repos():
        # Resolve to compare reliably: git reports real paths (e.g. /private
        # on macOS) that may differ textually from the configured location.
        repo_worktrees = (worktrees_root / repo).resolve()
        try:
            paths = git_ops.list_worktrees(config.workspaces_dir() / repo)
        except git_ops.GitError:
            continue
        for path in paths:
            path = path.resolve()
            # Keep only the disposable worktrees vv created, not the main clone.
            if path.parent == repo_worktrees:
                found.append((repo, path.name, path))
    for path in sorted(config.chats_dir().iterdir()):
        if path.is_dir():
            found.append((CHATS, path.name, path))
    # repo ascending, creation time descending (newest first), name as tiebreak.
    return sorted(found, key=lambda w: (w[0], -_created_ts(w[2]), w[1]))


def _pick_agent(default: str) -> str | None:
    """Ask which agent CLI to launch, listing known agents found on PATH.

    The configured default is offered first (even if not detected on PATH);
    an "other" entry accepts any command. Returns None if the user cancels.
    """
    # Default first, then the rest of the detected agents, de-duplicated.
    ordered: list[str] = []
    for candidate in (default, *agents.installed_agents()):
        if candidate not in ordered:
            ordered.append(candidate)

    custom = object()  # sentinel value that cannot collide with a command
    choices = [
        questionary.Choice(
            title=f"{a}  (default)" if a == default else a, value=a
        )
        for a in ordered
    ]
    choices.append(questionary.Choice(title="other (enter a command)…", value=custom))

    picked = questionary.select(
        "Which agent CLI should this session run?", choices=choices
    ).ask()
    if picked is None:
        return None
    if picked is custom:
        entered = questionary.text("Agent command:", default=default).ask()
        entered = (entered or "").strip()
        return entered or None
    return picked


def _resume_worktree(name: str, worktree_path: Path, agent: str, bypass: bool) -> None:
    """Attach to the worktree's tmux session, creating it fresh if none is live.

    The worktree is the session: if vv already has a tmux session of this name
    we hand the terminal to it; otherwise we start one rooted at the worktree
    and launch ``agent``, just like a brand-new session but with existing state.
    When ``bypass`` is set, the agent is launched with permission prompts off.
    """
    if tmux_ops.session_exists(name):
        typer.secho(f"Joining live session '{name}'...", fg=typer.colors.CYAN)
    else:
        launch = agents.with_bypass(agent) if bypass else agent
        typer.secho(
            f"Starting tmux session '{name}' and launching {launch}...",
            fg=typer.colors.CYAN,
        )
        if not agents.is_installed(agent):
            typer.secho(
                f"  warning: '{agent}' was not found on PATH", fg=typer.colors.YELLOW
            )
        tmux_ops.create_session(name, worktree_path)
        tmux_ops.send_command(name, launch, cwd=worktree_path)

    typer.secho(f"  worktree: {worktree_path}", fg=typer.colors.GREEN)
    typer.secho(f"  session:  {name}", fg=typer.colors.GREEN)
    tmux_ops.attach(name, worktree_path)


def _new_worktree_session(
    repo_name: str,
    workspace: Path,
    agent: str,
    bypass: bool,
    name: str | None = None,
    pending_notes: "notes.Pending | None" = None,
) -> None:
    """Create a worktree + tmux session for an already-cloned repo and attach.

    With no ``name`` a random collision-free one is picked; an explicit ``name``
    (e.g. forwarded from a remote launcher via ``--name``) is used as-is but
    must not already be taken. Any ``pending_notes`` (from ``--title`` /
    ``--label``) are stamped on the new session before it is handed the terminal.
    """
    worktree_root = config.worktrees_dir() / repo_name

    taken: set[str] = set(tmux_ops.list_sessions())
    taken |= git_ops.existing_branches(workspace)
    if worktree_root.exists():
        taken |= {p.name for p in worktree_root.iterdir()}

    if name is None:
        name = names.random_name(taken)
    elif name in taken:
        raise _fail(f"session '{name}' already exists")
    worktree_path = worktree_root / name

    # Branch off the *latest* remote state, not whatever the clone last saw.
    # Every create flow lands here, so the fetch belongs here rather than in
    # any one of them: a session cut from a stale origin/main starts behind and
    # has to be caught up by hand later. Best-effort — an unreachable remote is
    # no reason to refuse to make a session, it just means branching off the
    # refs already on disk.
    typer.secho(f"Fetching latest for '{repo_name}'...", fg=typer.colors.CYAN)
    try:
        git_ops.fetch(workspace)
    except git_ops.GitError as exc:
        typer.secho(f"  (fetch failed, continuing: {exc})", fg=typer.colors.YELLOW)

    start_ref = git_ops.default_start_ref(workspace)
    typer.secho(
        f"Creating worktree '{name}' (branch off {start_ref})...",
        fg=typer.colors.CYAN,
    )
    git_ops.add_worktree(workspace, worktree_path, branch=name, start_ref=start_ref)

    _note_new_session(repo_name, name, pending_notes)
    _resume_worktree(name, worktree_path, agent, bypass)


def _note_new_session(
    repo: str, name: str, pending: "notes.Pending | None"
) -> None:
    """Stamp ``--title`` / ``--label`` onto a session vv just created (best-effort).

    A bad label spec is reported but never sinks the session that has already
    been created — the user can always re-annotate it afterwards.
    """
    if not pending:
        return
    if pending.title is not None:
        title = notes.set_title(repo, name, pending.title)
        if title:
            typer.secho(f"  title:    {title}", fg=typer.colors.GREEN)
    if not pending.label_specs:
        return
    try:
        current, _added, _removed = notes.apply_labels(
            repo, name, list(pending.label_specs)
        )
    except notes.LabelError as exc:
        typer.secho(f"  (labels not applied: {exc})", fg=typer.colors.YELLOW)
        return
    if current:
        typer.secho(f"  labels:   {', '.join(current)}", fg=typer.colors.GREEN)


def _new_chat_session(
    agent: str,
    bypass: bool,
    name: str | None = None,
    pending_notes: "notes.Pending | None" = None,
) -> None:
    """Create an empty chat-only session dir and attach an agent to it.

    Chat sessions are not backed by a git worktree — they are just a plain
    directory under :func:`config.chats_dir`, intended for persistent agent
    conversations that don't need (or want) version control.
    """
    chats_root = config.chats_dir()

    # Avoid colliding with any existing tmux session or vv session name.
    taken: set[str] = set(tmux_ops.list_sessions())
    taken |= {existing for _repo, existing, _path in _list_worktrees()}

    if name is None:
        name = names.random_name(taken)
    elif name in taken:
        raise _fail(f"session '{name}' already exists")
    chat_path = chats_root / name
    chat_path.mkdir(parents=True)

    typer.secho(f"Creating chat session '{name}'...", fg=typer.colors.CYAN)
    _note_new_session(CHATS, name, pending_notes)
    _resume_worktree(name, chat_path, agent, bypass)


def _start_from_url(
    repo_url: str,
    agent: str,
    bypass: bool,
    name: str | None = None,
    pending_notes: "notes.Pending | None" = None,
) -> None:
    """Clone the repo if needed, then create a new worktree session."""
    repo_name = git_ops.repo_name_from_url(repo_url)
    workspace = config.workspaces_dir() / repo_name

    if workspace.exists():
        # No fetch here — _new_worktree_session fetches for every create flow.
        typer.secho(f"Repo '{repo_name}' already cloned.", fg=typer.colors.CYAN)
    else:
        typer.secho(f"Cloning '{repo_name}'...", fg=typer.colors.CYAN)
        git_ops.clone(repo_url, workspace)

    # A freshly-created remote has no commits, so its HEAD is unborn and there
    # is nothing to branch a worktree from. Seed the default branch with an
    # empty root commit and push it, so worktrees branch off main as usual
    # (rather than a disposable worktree branch becoming the repo's first
    # branch). The push is best-effort — a local commit alone is enough to
    # branch from if the remote can't be reached.
    if not git_ops.has_head_commit(workspace):
        typer.secho(
            "Empty repo — seeding the default branch with an initial commit...",
            fg=typer.colors.CYAN,
        )
        git_ops.seed_initial_commit(workspace)
        try:
            git_ops.push_current(workspace)
        except git_ops.GitError as exc:
            typer.secho(
                f"  (push failed, continuing with local commit: {exc})",
                fg=typer.colors.YELLOW,
            )

    _new_worktree_session(repo_name, workspace, agent, bypass, name, pending_notes)


def _launch_remote(
    repo_url: str | None,
    chat: bool,
    agent: str | None,
    ask: bool | None,
    name: str | None,
    pending_notes: "notes.Pending | None" = None,
) -> None:
    """Forward this invocation to vv on the configured remote, inside a cmux tab.

    Local vv does no git/tmux work in remote mode: it opens a cmux workspace
    that SSHes to the server and runs vv there with the same intent. Bare ``vv``
    forwards nothing extra, so the remote's own interactive TUI opens in the
    tab; a repo URL / ``--chat`` runs the remote create flow. The ``agent`` /
    ``ask`` flags are forwarded only when explicitly set, leaving the remote's
    own config to decide otherwise.
    """
    remote_cfg = config.configured_remote()
    if remote_cfg is None:
        raise _fail("remote mode is on but no [remote] is configured")

    # Mirror the name only when a session is unambiguously created up front; a
    # bare `vv` opens the remote TUI, which names its own sessions.
    session_name = name or (remote.gen_name() if (repo_url or chat) else None)

    forward: list[str] = []
    if session_name:
        forward += ["--name", session_name]
    forward.append("--local")  # the remote must never recurse into remote mode
    if agent is not None:
        forward += ["--agent", agent]
    if ask is True:
        forward.append("--ask")
    elif ask is False:
        forward.append("--no-ask")
    # "=" form throughout so a removal spec's leading '-' (or a title starting
    # with one) can't be read as a flag by the remote's parser.
    if pending_notes is not None and pending_notes.title is not None:
        forward.append(f"--title={pending_notes.title}")
    for spec in (pending_notes.label_specs if pending_notes else ()):
        forward.append(f"--label={spec}")
    if chat:
        forward.append("--chat")
    if repo_url:
        forward.append(repo_url)

    title = session_name or remote_cfg.host
    remote.launch(remote_cfg, remote_argv=forward, title=title)


def _resume_session(
    name: str, path: Path, default_agent: str, live: set[str], bypass: bool
) -> None:
    """Resume a worktree's session, picking an agent if it must be restarted."""
    # A live session is just re-attached; only a dead one needs an agent, so
    # ask which CLI to relaunch it with (vv does not track the prior choice).
    if name in live:
        agent = default_agent
    else:
        agent = _pick_agent(default_agent)
        if agent is None:
            return
    _resume_worktree(name, path, agent, bypass)


def _delete_chat(name: str, path: Path, live: set[str]) -> bool:
    """Delete a chat session dir, warning first if it has any contents.

    Returns whether it was deleted (``False`` when the user declined).
    """
    if any(path.iterdir()):
        typer.secho(
            f"chat '{name}' has files in it that would be lost.",
            fg=typer.colors.YELLOW,
        )
        confirmed = questionary.confirm(
            "Delete it and everything in it? This cannot be undone.", default=False
        ).ask()
        if not confirmed:
            typer.secho("Cancelled — chat kept.", fg=typer.colors.CYAN)
            return False

    _remove_session(CHATS, name, path, live)
    typer.secho(f"Deleted chat '{name}'.", fg=typer.colors.GREEN)
    return True


def _remove_session(repo: str, name: str, path: Path, live: set[str]) -> None:
    """Tear a session down — tmux session, worktree/dir, branch, notes. No prompts.

    The mechanics only: the caller owns the warning and confirmation (per-session
    in :func:`_delete_session`, once for the whole batch in
    :func:`_sweep_stale_sessions`). Raises :class:`git_ops.GitError` on a failed
    git step.
    """
    # The session's working directory is about to vanish; close it first.
    if name in live:
        tmux_ops.kill_session(name)
    if repo == CHATS:
        shutil.rmtree(path)
    else:
        workspace = config.workspaces_dir() / repo
        git_ops.remove_worktree(workspace, path, force=True)
        git_ops.delete_branch(workspace, name, force=True)
    notes.forget(repo, name)


def _delete_session(repo: str, name: str, path: Path, live: set[str]) -> bool:
    """Delete a session, warning first if it holds work that would be lost.

    Returns whether it was deleted (``False`` when the user declined).
    """
    if repo == CHATS:
        return _delete_chat(name, path, live)

    risks = _work_at_risk(path)
    if risks:
        typer.secho(f"'{repo}/{name}' has work that would be lost:", fg=typer.colors.YELLOW)
        for risk in risks:
            typer.secho(f"  - {risk}", fg=typer.colors.YELLOW)
        confirmed = questionary.confirm(
            "Delete it anyway? This cannot be undone.", default=False
        ).ask()
        if not confirmed:
            typer.secho("Cancelled — worktree kept.", fg=typer.colors.CYAN)
            return False

    _remove_session(repo, name, path, live)
    typer.secho(f"Deleted worktree '{repo}/{name}'.", fg=typer.colors.GREEN)
    return True


def _work_at_risk(path: Path) -> list[str]:
    """Describe the work in a git session that deleting it would lose.

    Empty when there is nothing uncommitted and nothing unpushed. Raises
    :class:`git_ops.GitError` if the session can't be inspected — callers must
    decide what an unknown state means (a delete asks, the sweep skips).
    """
    risks: list[str] = []
    if git_ops.is_dirty(path):
        risks.append("uncommitted changes in the working tree")
    unpushed = git_ops.unpushed_count(path)
    if unpushed:
        plural = "" if unpushed == 1 else "s"
        risks.append(f"{unpushed} commit{plural} not pushed to any remote")
    return risks


def _session_from_cwd() -> tuple[str, str, Path] | None:
    """Return the ``(repo, name, path)`` of the session the cwd is inside, if any.

    vv sessions run rooted at their worktree/chat dir, so the current directory
    identifies the session you are in — including from a subdirectory of it.
    Returns ``None`` when the cwd belongs to no session.
    """
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        return None
    for repo, name, path in _list_worktrees():
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if cwd == resolved or resolved in cwd.parents:
            return repo, name, path
    return None


def _find_session(name: str) -> tuple[str, str, Path] | None:
    """Return the ``(repo, name, path)`` of the session called ``name``, if any."""
    for session in _list_worktrees():
        if session[1] == name:
            return session
    return None


def _apply_notes(pending: notes.Pending, name: str | None) -> None:
    """Set the title and/or labels on an existing session.

    Backs ``vv --title TEXT`` / ``vv --label TAG``: targets the session named by
    ``--name``, else the one the cwd is inside — so inside a session you can
    just annotate it. Each label spec adds a label, or removes it when prefixed
    with ``-``; a blank title clears the title.
    """
    if name:
        session = _find_session(name)
        if session is None:
            raise _fail(f"no session named '{name}'")
    else:
        session = _session_from_cwd()
        if session is None:
            raise _fail(
                "not inside a vv session — run this from a session, or pass --name NAME"
            )

    repo, session_name, _path = session
    if pending.title is not None:
        title = notes.set_title(repo, session_name, pending.title)
        typer.secho(
            f"  title: {title}" if title else "  title cleared",
            fg=typer.colors.GREEN if title else typer.colors.YELLOW,
        )

    if pending.label_specs:
        try:
            current, added, removed = notes.apply_labels(
                repo, session_name, list(pending.label_specs)
            )
        except notes.LabelError as exc:
            raise _fail(str(exc)) from exc
        for label in added:
            typer.secho(f"  + {label}", fg=typer.colors.GREEN)
        for label in removed:
            typer.secho(f"  - {label}", fg=typer.colors.YELLOW)
        shown = ", ".join(current) if current else "(none)"
        typer.secho(f"  labels: {shown}", fg=typer.colors.CYAN)

    typer.secho(f"Session '{repo}/{session_name}' updated.", fg=typer.colors.CYAN)


def _session_summaries(
    default_agent: str, worktrees: list[tuple[str, str, Path]]
) -> dict[tuple[str, str], str]:
    """Return a one-line summary of each session, keyed by ``(repo, name)``.

    Uses the config's ``summary_agent`` (falling back to the session agent) to
    describe what each worktree/chat is working on. Summaries are cached on disk
    and only regenerated for sessions whose activity fingerprint has changed
    since last time (i.e. that have been opened/worked in) — so a menu open is
    cheap when nothing has moved. Best-effort: if the summary agent can't run
    non-interactively, or a summary fails, that session shows no description.
    """
    summary_agent = config.configured_summary_agent() or default_agent
    if not summary.can_summarize(summary_agent):
        return {}

    cache = summary.load_cache()
    fingerprints = {
        (repo, name): summary.session_fingerprint(path)
        for repo, name, path in worktrees
    }
    results: dict[tuple[str, str], str] = {}
    stale: dict[tuple[str, str], Path] = {}
    for repo, name, path in worktrees:
        key = (repo, name)
        entry = cache.get(f"{repo}/{name}")
        if (
            entry
            and entry.get("agent") == summary_agent
            and entry.get("fingerprint") == fingerprints[key]
            and entry.get("summary")
        ):
            results[key] = entry["summary"]
        else:
            stale[key] = path

    if stale:
        typer.secho(
            f"Summarizing {len(stale)} session(s) with {summary_agent}...",
            fg=typer.colors.CYAN,
        )
        results.update(summary.summarize_all(summary_agent, stale))

    # Rewrite the cache to exactly the current sessions (pruning deleted ones),
    # stamping freshly generated summaries with their new fingerprint.
    new_cache: dict[str, dict] = {}
    for repo, name, _path in worktrees:
        key = (repo, name)
        if key in results:
            new_cache[f"{repo}/{name}"] = {
                "agent": summary_agent,
                "fingerprint": fingerprints[key],
                "summary": results[key],
            }
    summary.save_cache(new_cache)
    return results


def _menu_list_sessions(default_agent: str, bypass: bool) -> None:
    """List existing worktrees as cards; resume or delete the chosen one.

    A delete **loops back** to the freshly-rebuilt list instead of leaving the
    menu, so a run of stale sessions can be cleaned up in one visit; the cursor
    lands on the session that took the deleted one's place. **Shift+X** sweeps
    every stale session at once (:func:`_sweep_stale_sessions`) and also loops
    back. Resuming or cancelling still leaves.
    """
    focus: tuple[str, str] | None = None  # session to point at on re-entry
    deleted_any = False
    while True:
        worktrees = _list_worktrees()
        if not worktrees:
            if deleted_any:
                typer.secho("No sessions left.", fg=typer.colors.CYAN)
            else:
                typer.secho(
                    "No worktrees yet. Choose a repo to start one.",
                    fg=typer.colors.YELLOW,
                )
            return
        live = set(tmux_ops.list_sessions())
        summaries = _session_summaries(default_agent, worktrees)

        # PR status: serve whatever is cached instantly, then refresh the rest in
        # the background while the menu is open (see `_pick_session`), so opening
        # the view never blocks on `gh`.
        session_paths = {(repo, name): path for repo, name, path in worktrees}
        pr_snapshot = pr.Snapshot(session_paths)
        pr_cached = pr_snapshot.cached
        pr_stale = pr_snapshot.stale_keys

        note_store = notes.all_notes()

        cards: list[dict] = []
        choices: list[questionary.Choice] = []
        card_by_key: dict[tuple[str, str], dict] = {}
        for repo, name, path in worktrees:
            is_git = (path / ".git").exists()
            key = (repo, name)
            note = note_store.get(notes.session_id(repo, name), notes.Note())
            card = {
                "running": name in live,
                "title": note.title,          # user-set; sits above the summary
                "summary": summaries.get(key),
                "labels": note.labels,
                "branch": name if is_git else None,  # vv's worktree branch is its name
                "dirty": _worktree_dirty(path) if is_git else False,
                "folder": f"{repo}/{name}",
                "pr": pr_cached.get(key),
                "pr_pending": key in pr_stale,  # awaiting a background refresh
                "when": _relative_time(_created_ts(path)),
            }
            cards.append(card)
            card_by_key[key] = card
            choices.append(questionary.Choice(title=f"{repo}/{name}", value=(repo, name, path)))

        action, value = _pick_session(
            "Sessions  ·  enter to resume · x to delete · X to clean up stale",
            choices,
            cards,
            pr_snapshot,
            card_by_key,
            _card_theme(),
            focus=_focus_value(choices, focus),
        )
        if action == "cancel":
            return
        repo, name, path = value
        if action == "sweep":
            # Whatever the sweep took, the cursor tries to stay where it was; a
            # swept-away session just falls back to the top of the rebuilt list.
            focus = (repo, name)
            if _sweep_stale_sessions(worktrees, live, card_by_key):
                deleted_any = True
            continue
        if action != "delete":
            _resume_session(name, path, default_agent, live, bypass)
            return

        order = [(r, n) for r, n, _ in worktrees]
        if _delete_session(repo, name, path, live):
            deleted_any = True
            focus = _next_focus(order, (repo, name))
        else:
            focus = (repo, name)  # kept — stay on it


def _focus_value(choices: list, focus: tuple[str, str] | None) -> object | None:
    """Map a ``(repo, name)`` key to the matching choice value, if it is still there."""
    if focus is None:
        return None
    return next((c.value for c in choices if (c.value[0], c.value[1]) == focus), None)


def _next_focus(
    order: list[tuple[str, str]], removed: tuple[str, str]
) -> tuple[str, str] | None:
    """The session the cursor should land on after ``removed`` is deleted.

    Prefers the one below it (which slides up into its slot), else the one above,
    else nothing — the same feel as deleting a line in an editor.
    """
    try:
        index = order.index(removed)
    except ValueError:
        return None
    if index + 1 < len(order):
        return order[index + 1]
    if index:
        return order[index - 1]
    return None


# --- bulk cleanup of stale sessions (Shift+X) --------------------------------

#: Why a session was picked up by the sweep. A merged PR means the work landed;
#: "untouched" means the branch never diverged from where it was cut and has
#: nothing uncommitted — there is nothing in it to lose either way.
_REASON_MERGED = "PR #{number} merged"
_REASON_UNTOUCHED = "no local changes"


@dataclass
class _Stale:
    """A session the sweep proposes deleting, with why and what it would cost."""

    repo: str
    name: str
    path: Path
    reason: str
    #: Work that would still be lost (a merged PR can sit on unpushed commits),
    #: plus "session is running" — shown so the confirmation is informed.
    risks: list[str]

    @property
    def key(self) -> tuple[str, str]:
        return self.repo, self.name


def _sweep_stale_sessions(
    worktrees: list[tuple[str, str, Path]], live: set[str], card_by_key: dict
) -> int:
    """Find every stale session, list it for review, and delete the lot on confirm.

    "Stale" is :func:`_classify_stale`: a merged PR, or a branch with no local
    changes at all — never one with an uncommitted working tree. The list is
    printed first — with each session's headline and
    anything that would still be lost — and nothing is touched until a single
    confirmation covers the whole batch. Returns how many were deleted.
    """
    statuses = _resolve_pr_status(worktrees)
    candidates = [
        stale
        for repo, name, path in worktrees
        if (stale := _classify_stale(repo, name, path, live, statuses)) is not None
    ]
    if not candidates:
        typer.secho(
            "Nothing looks stale — every session has work in it or a PR still open.",
            fg=typer.colors.CYAN,
        )
        return 0

    typer.secho(
        f"\n{len(candidates)} of {len(worktrees)} sessions look stale:",
        fg=typer.colors.YELLOW,
        bold=True,
    )
    for stale in candidates:
        typer.secho(f"  {stale.repo}/{stale.name}", fg=typer.colors.WHITE, bold=True, nl=False)
        typer.secho(f"  ·  {stale.reason}", fg=typer.colors.GREEN, nl=False)
        typer.secho(
            f"  ·  {', '.join(stale.risks)}" if stale.risks else "",
            fg=typer.colors.YELLOW,
        )
        headline = _stale_headline(card_by_key.get(stale.key))
        if headline:
            typer.secho(f"      {headline}", fg=typer.colors.BRIGHT_BLACK)
    typer.echo()

    confirmed = questionary.confirm(
        f"Delete all {len(candidates)} of them? This cannot be undone.", default=False
    ).ask()
    if not confirmed:
        typer.secho("Cancelled — nothing deleted.", fg=typer.colors.CYAN)
        return 0

    deleted = 0
    for stale in candidates:
        try:
            _remove_session(stale.repo, stale.name, stale.path, live)
        except (git_ops.GitError, tmux_ops.TmuxError, OSError) as exc:
            # One stubborn session mustn't strand the rest of the batch.
            typer.secho(f"  ! kept {stale.repo}/{stale.name}: {exc}", fg=typer.colors.RED)
            continue
        deleted += 1
    typer.secho(
        f"Deleted {deleted} of {len(candidates)} stale sessions.", fg=typer.colors.GREEN
    )
    return deleted


def _classify_stale(
    repo: str, name: str, path: Path, live: set[str], statuses: dict
) -> _Stale | None:
    """Return why this session is stale, or ``None`` to leave it alone.

    Two reasons, both meaning "nothing here is waiting on you":

    * its **PR is merged** — the work landed, so the branch has served its purpose
      (still reported with anything unpushed left behind, since a merged PR says
      nothing about commits that never went up);
    * it has **no local changes** — clean working tree, nothing unpushed, and no
      commits of its own beyond where the branch was cut.

    An **uncommitted working tree vetoes both**: those changes exist nowhere else,
    so no amount of merged-PR evidence makes them safe to bin. Such a session is
    never proposed in bulk — it can still be deleted one at a time through
    :func:`_delete_session`, where the warning names what is being lost.

    Deliberately conservative everywhere else: chats are never swept (no branch
    and no PR to judge them by), a session that is *running* is never swept for
    being untouched (that is the one you just opened), an open PR keeps its
    session no matter how clean it is (that work is under review), and a session
    git can't answer for is skipped rather than assumed empty.
    """
    if repo == CHATS:
        return None
    pr_info = statuses.get((repo, name))
    merged = bool(pr_info) and pr_info.get("state") == "merged"

    try:
        if git_ops.is_dirty(path):
            return None  # uncommitted work is unrecoverable -> never swept
        risks = _work_at_risk(path)
        untouched = not risks and _commits_ahead(repo, path) == 0
    except git_ops.GitError:
        return None  # can't vouch for it -> don't offer it up

    if merged:
        if name in live:
            risks = [*risks, "session is running"]
        return _Stale(repo, name, path, _REASON_MERGED.format(number=pr_info["number"]), risks)
    if untouched and name not in live:
        return _Stale(repo, name, path, _REASON_UNTOUCHED, [])
    return None


def _commits_ahead(repo: str, path: Path) -> int:
    """Commits on the session's branch that its repo's default branch doesn't have."""
    base = git_ops.default_start_ref(config.workspaces_dir() / repo)
    return git_ops.commits_ahead(path, base)


def _resolve_pr_status(worktrees: list[tuple[str, str, Path]]) -> dict:
    """PR status for every git session, fetching what the cache doesn't know.

    The cards' background refresh is best-effort and may not have landed (or may
    have been cut short when the menu closed), and treating an unknown PR as "not
    merged" would silently under-clean. So the sweep blocks on the fetch — the one
    place in vv where waiting on ``gh`` is the right trade.
    """
    snapshot = pr.Snapshot({(repo, name): path for repo, name, path in worktrees})
    statuses = dict(snapshot.cached)
    pending = snapshot.stale_keys
    if pending:
        typer.secho(
            f"Checking {len(pending)} session(s) for merged PRs…", fg=typer.colors.CYAN
        )
        snapshot.refresh(lambda key, pr_info: statuses.__setitem__(key, pr_info)).join()
    return statuses


def _stale_headline(card: dict | None) -> str:
    """The card's title (else its summary), trimmed to one short line for the list."""
    if not card:
        return ""
    text = (card.get("title") or card.get("summary") or "").strip()
    return textwrap.shorten(text, width=64, placeholder="…") if text else ""


# --- session cards ----------------------------------------------------------

#: Fixed inner chrome per card row: "│ " (2) on the left, " │" (2) on the right.
_CARD_CHROME = 4
#: Left gutter reserved for the selection bar ("▌ " / "  ").
_CARD_GUTTER = 2
#: Right slack so an over-wide glyph can't clip card content against the border.
_CARD_RIGHT_MARGIN = 2
#: Cushion kept *inside* the card before the right-aligned timestamp, so a glyph
#: a terminal renders wider than we measured eats slack instead of clipping text.
_CARD_TEXT_SLACK = 3
#: Cap on card width so lines don't sprawl on a very wide terminal.
_CARD_MAX_WIDTH = 74

#: Default card glyphs. Each is overridable from the config's ``[cards.glyphs]``
#: table (see :func:`config.configured_card_glyphs`). Keep overrides single-cell
#: so the layout stays aligned (``separator`` is the exception — it carries its
#: own spaces).
_DEFAULT_GLYPHS: dict[str, str] = {
    "running": "▸",        # live session (filled right triangle)
    "idle": "▹",           # idle session (outline right triangle)
    "dirty": "✱",          # uncommitted/unpushed marker after the branch
    "separator": " · ",    # between branch and folder
    "label": "#",          # prefix on each user-assigned label
    "label_gap": "  ",     # between labels (wide enough to read multi-word ones)
    "chat": "❝",           # chat-session line
    "pr_open": "○",        # open PR (outline circle)
    "pr_draft": "◌",       # draft PR (dotted circle)
    "pr_merged": "●",      # merged PR (filled circle)
    "pr_closed": "⊘",      # closed PR
    "pr_none": "○",        # git session with no PR
    "pr_checking": "⋯",    # PR status refresh in flight
    "check_passing": "✓",  # CI checks passing
    "check_failing": "✗",  # CI checks failing
    "check_pending": "◔",  # CI checks running
    "select_pointer": "❯", # selected card, top row
    "select_bar": "▌",     # selected card, other rows
}

#: Default card colors (prompt_toolkit style strings), overridable from the
#: config's ``[cards.colors]`` table (see :func:`config.configured_card_colors`).
_DEFAULT_COLORS: dict[str, str] = {
    "border": "ansibrightblack",
    "bar": "ansicyan bold",
    "running": "ansigreen bold",
    "idle": "ansibrightblack",
    "title": "bold",
    "summary": "ansibrightblack",  # generated summary, under a user-set title
    "label": "ansimagenta",
    "branch": "ansicyan",
    "dirty": "ansiyellow bold",
    "folder": "ansibrightblack",
    "time": "ansibrightblack",
    "selection": "bg:#334155",
    "pr_pass": "ansigreen",
    "pr_fail": "ansired",
    "pr_pending": "ansiyellow",
    "pr_open": "ansicyan",
    "pr_draft": "ansibrightblack",
    "pr_merged": "ansimagenta",
    "pr_closed": "ansired",
    "pr_none": "ansibrightblack",
}


@dataclass(frozen=True)
class CardTheme:
    """The glyphs and colors used to render session cards."""

    glyphs: dict[str, str]
    colors: dict[str, str]


#: Theme with everything at its default; the fallback when no config overrides.
_DEFAULT_THEME = CardTheme(_DEFAULT_GLYPHS, _DEFAULT_COLORS)


def _card_theme() -> CardTheme:
    """Build the card theme, layering the config's overrides over the defaults."""
    return CardTheme(
        glyphs={**_DEFAULT_GLYPHS, **config.configured_card_glyphs()},
        colors={**_DEFAULT_COLORS, **_valid_colors(config.configured_card_colors())},
    )


def _valid_colors(overrides: dict[str, str]) -> dict[str, str]:
    """Keep only color overrides prompt_toolkit can parse.

    A malformed style string (e.g. a typo'd color name) raises when
    prompt_toolkit resolves it *at render time*, which would crash the menu — so
    we probe each value here and silently drop the bad ones, leaving the default.
    """
    from prompt_toolkit.styles import Style

    good: dict[str, str] = {}
    for key, value in overrides.items():
        try:
            Style([("_probe", value)]).get_attrs_for_style_str("class:_probe")
        except ValueError:
            continue  # invalid style string -> fall back to the default for this key
        good[key] = value
    return good


def _worktree_dirty(path: Path) -> bool:
    """True if the worktree has uncommitted or unpushed work (the ``*`` marker).

    Best-effort and local-only (no network): any git error just means no marker.
    """
    try:
        return git_ops.is_dirty(path) or git_ops.unpushed_count(path) > 0
    except git_ops.GitError:
        return False


def _relative_time(ts: float) -> str:
    """Format a Unix timestamp as a compact relative age, e.g. ``3d ago``."""
    import time

    secs = max(0.0, time.time() - ts)
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"
    return "just now"


#: PR state -> (glyph key, style class). Draft/merged/closed carry no check
#: overlay. The default circle family (dotted=draft, outline=open, filled=merged)
#: plus a circled slash for closed is themeable; glyphs come from the theme.
_PR_STATE_STYLE = {
    "draft": ("pr_draft", "class:card.pr.draft"),
    "merged": ("pr_merged", "class:card.pr.merged"),
    "closed": ("pr_closed", "class:card.pr.closed"),
}
#: Check rollup -> (glyph key, word, style class) for an open PR.
_PR_CHECK_STYLE = {
    "passing": ("check_passing", "passing", "class:card.pr.pass"),
    "failing": ("check_failing", "failing", "class:card.pr.fail"),
    "pending": ("check_pending", "pending", "class:card.pr.pending"),
}


def _pr_segment(card: dict, theme: "CardTheme | None" = None) -> tuple[str, str]:
    """Return ``(text, style_class)`` for a card's PR-status line."""
    g = (theme or _DEFAULT_THEME).glyphs
    if card["branch"] is None:
        return f"{g['chat']} chat session", "class:card.pr.none"
    pr_info = card["pr"]
    if not pr_info and card.get("pr_pending"):
        return f"{g['pr_checking']} checking…", "class:card.pr.none"  # refresh in flight
    if not pr_info:
        return f"{g['pr_none']} no open PR", "class:card.pr.none"
    number = pr_info.get("number")
    state = pr_info.get("state", "open")
    if state in _PR_STATE_STYLE:  # draft / merged / closed carry no check overlay
        glyph_key, style = _PR_STATE_STYLE[state]
        return f"{g[glyph_key]} PR #{number} {state}", style
    # Open: the state glyph, then the check rollup drives the trailing mark +
    # color (or a plain "open" when there are no checks yet).
    check = _PR_CHECK_STYLE.get(pr_info.get("checks"))
    if check is None:
        return f"{g['pr_open']} PR #{number} open", "class:card.pr.open"
    glyph_key, word, style = check
    return f"{g['pr_open']} PR #{number} {g[glyph_key]} {word}", style


def _card_lines(
    card: dict, width: int, theme: "CardTheme | None" = None
) -> list[list[tuple[str, str]]]:
    """Render one session card as a list of rows (each a list of style segments).

    Pure and selection-agnostic: :func:`_render_cards` applies the selection
    highlight afterward. ``width`` is the full terminal width; the card fills it
    (minus the selection gutter) up to :data:`_CARD_MAX_WIDTH`.
    """
    theme = theme or _DEFAULT_THEME
    g = theme.glyphs
    # Leave a right margin so a glyph a terminal happens to render wider than one
    # cell (some phone fonts do) eats slack instead of clipping the card content.
    card_width = max(min(width - _CARD_GUTTER - _CARD_RIGHT_MARGIN, _CARD_MAX_WIDTH), 24)
    inner = card_width - _CARD_CHROME
    border = "class:card.border"

    def content_row(segments: list[tuple[str, str]]) -> list[tuple[str, str]]:
        visible = sum(len(text) for _style, text in segments)
        pad = " " * max(0, inner - visible)
        return [(border, "│ "), *segments, ("", pad), (border, " │")]

    rows: list[list[tuple[str, str]]] = [[(border, "╭" + "─" * (card_width - 2) + "╮")]]

    dot_style = "class:card.dot.run" if card["running"] else "class:card.dot.idle"
    dot = g["running"] if card["running"] else g["idle"]

    # The headline is the user's own title when they set one, else the generated
    # summary; the dot leads it. A title doesn't replace the summary — the
    # summary follows underneath it, in its own (quieter) style.
    title = card.get("title")
    headline = title or card["summary"] or "(no summary yet)"
    body_lines = textwrap.wrap(card["summary"] or "", inner - 2) if title else []

    first = True
    for line in textwrap.wrap(headline, inner - 2) or [headline]:
        prefix = [(dot_style, f"{dot} ")] if first else [("", "  ")]
        rows.append(content_row([*prefix, ("class:card.title", line)]))
        first = False
    for line in body_lines:
        rows.append(content_row([("", "  "), ("class:card.summary", line)]))

    # User-assigned labels sit just under the title, indented to line up with it.
    chips = g["label_gap"].join(
        f"{g['label']}{label}" for label in card.get("labels") or []
    )
    for line in textwrap.wrap(chips, inner - 2):
        rows.append(content_row([("", "  "), ("class:card.label", line)]))

    if card["branch"]:
        # branch [dirty] <separator> folder. No leading glyph: an uncommon symbol
        # like ⎇ renders wide on some fonts and clips the branch name.
        location = [("class:card.branch", card["branch"])]
        if card.get("dirty"):
            location.append(("class:card.dirty", g["dirty"]))
        location.append(("class:card.folder", f"{g['separator']}{card['folder']}"))
    else:  # chat sessions have no git branch — just show the folder
        location = [("class:card.folder", card["folder"])]
    rows.append(content_row(location))

    pr_text, pr_style = _pr_segment(card, theme)
    when = card["when"]
    # Right-align the timestamp, but keep _CARD_TEXT_SLACK cells of cushion before
    # the border. A glyph the terminal renders wider than we measured (some phone
    # fonts substitute a wide emoji for an uncommon symbol) then eats that slack
    # instead of clipping the PR text. Truncate only if genuinely too long.
    usable = inner - _CARD_TEXT_SLACK
    gap = usable - len(pr_text) - len(when)
    if gap < 1:
        pr_text = pr_text[: max(0, usable - len(when) - 2)] + "…"
        gap = usable - len(pr_text) - len(when)
    rows.append(
        content_row(
            [(pr_style, pr_text), ("", " " * max(1, gap)), ("class:card.time", when)]
        )
    )

    rows.append([(border, "╰" + "─" * (card_width - 2) + "╯")])
    return rows


def _render_cards(
    cards: list[dict], pointed_at: int, width: int, theme: "CardTheme | None" = None
) -> list[tuple[str, str]]:
    """Build the full formatted-text token stream for every card.

    The card at ``pointed_at`` gets a left selection bar and the ``card.sel``
    background layered onto *every* segment of every row — including the padding
    and the gutter — so the whole card is washed evenly rather than only where
    there is text. The cursor sentinel is placed on the card's middle row so
    prompt_toolkit's scrolling keeps the entire card on screen (see
    :func:`_pick_session`).
    """
    theme = theme or _DEFAULT_THEME
    pointer, bar = theme.glyphs["select_pointer"], theme.glyphs["select_bar"]
    tokens: list[tuple[str, str]] = []
    for index, card in enumerate(cards):
        selected = index == pointed_at
        rows = _card_lines(card, width, theme)
        cursor_row = len(rows) // 2
        for row_number, row in enumerate(rows):
            if selected and row_number == cursor_row:
                tokens.append(("[SetCursorPosition]", ""))
            gutter = (f"{pointer} " if row_number == 0 else f"{bar} ") if selected else "  "
            tokens.append((_sel("class:card.bar", selected), gutter))
            for style, text in row:
                tokens.append((_sel(style, selected), text))
            tokens.append(("", "\n"))
    return tokens


def _sel(style: str, selected: bool) -> str:
    """Layer the selection background onto ``style`` when the card is selected.

    Applies to empty styles too (``"" -> "class:card.sel"``) so the padding
    between text and border is washed like everything else — otherwise short
    lines leave dark gaps in the highlight.
    """
    if not selected:
        return style
    return f"{style} class:card.sel".strip()


def _card_style(theme: "CardTheme | None" = None):
    """Build the prompt_toolkit style for session cards from the theme's colors.

    Each color is a prompt_toolkit style string; a bare color name is a
    foreground, ``bg:…`` a background (lazy import). A malformed value from the
    config would raise here, so we fall back to the defaults on any error rather
    than break the menu.
    """
    from prompt_toolkit.styles import Style

    c = (theme or _DEFAULT_THEME).colors
    rules = [
        ("card.border", c["border"]),
        ("card.bar", c["bar"]),
        ("card.dot.run", c["running"]),
        ("card.dot.idle", c["idle"]),
        ("card.title", c["title"]),
        ("card.summary", c["summary"]),
        ("card.label", c["label"]),
        ("card.branch", c["branch"]),
        ("card.dirty", c["dirty"]),
        ("card.folder", c["folder"]),
        ("card.pr.pass", c["pr_pass"]),
        ("card.pr.fail", c["pr_fail"]),
        ("card.pr.pending", c["pr_pending"]),
        ("card.pr.open", c["pr_open"]),
        ("card.pr.draft", c["pr_draft"]),
        ("card.pr.merged", c["pr_merged"]),
        ("card.pr.closed", c["pr_closed"]),
        ("card.pr.none", c["pr_none"]),
        ("card.time", c["time"]),
        ("card.sel", c["selection"]),
    ]
    return Style(rules)


def _pick_session(
    message: str,
    choices: list,
    cards: list[dict],
    pr_snapshot: "pr.Snapshot | None" = None,
    card_by_key: dict | None = None,
    theme: "CardTheme | None" = None,
    focus: object | None = None,
) -> tuple[str, object]:
    """Show the session list as cards; return ``(action, value)`` like :func:`_pick_with_delete`.

    Reuses questionary's ``select`` (navigation, Enter, and our ``x``-to-delete
    binding) but replaces the per-choice renderer with :func:`_render_cards` so
    each row is a bordered, colored card whose selected one is highlighted. The
    render function is swapped onto the control (``control.text``), which
    prompt_toolkit re-invokes on every keystroke, so it always reflects the live
    ``pointed_at``.

    When a :class:`pr.Snapshot` is given, its background refresh runs while the
    menu is open: as each session's live PR status lands, the matching card's
    ``pr`` is updated and the app is repainted (``app.invalidate()`` is thread
    safe). The menu stays fully responsive throughout; a ``stop`` event ends the
    refresh callbacks the moment the user leaves the view.

    ``focus`` is a choice value to start the cursor on (questionary's ``default``),
    which keeps the cursor in place when the view is re-entered after a delete.

    Three actions come back: ``"select"`` (Enter), ``"delete"`` (``x``, the
    pointed-at session) and ``"sweep"`` (``X``, bulk-clean the stale ones — it
    still reports the pointed-at session so the caller can restore the cursor).
    """
    from questionary.prompts.common import InquirerControl

    theme = theme or _DEFAULT_THEME
    question = questionary.select(
        message,
        choices=choices,
        style=_card_style(theme),
        pointer=None,
        instruction=" ",
        default=focus,
    )
    control = next(
        c
        for c in question.application.layout.find_all_controls()
        if isinstance(c, InquirerControl)
    )
    width = shutil.get_terminal_size().columns
    control.text = lambda: _render_cards(cards, control.pointed_at, width, theme)
    _keep_card_visible(question, cards, width)

    @question.application.key_bindings.add("x", eager=True)
    def _request_delete(event) -> None:
        event.app.exit(result=(_DELETE, control.get_pointed_at().value))

    @question.application.key_bindings.add("X", eager=True)
    def _request_sweep(event) -> None:
        event.app.exit(result=(_SWEEP, control.get_pointed_at().value))

    stop = threading.Event()
    if pr_snapshot is not None and card_by_key is not None:
        app = question.application

        def _on_pr(key, pr_info) -> None:
            card = card_by_key.get(key)
            if card is None or stop.is_set():
                return
            card["pr"] = pr_info
            card["pr_pending"] = False
            app.invalidate()  # thread-safe repaint; no-ops if the app has closed

        pr_snapshot.refresh(_on_pr, stop=stop)

    try:
        answer = question.ask()
    finally:
        stop.set()  # stop enriching cards once we leave the view

    if answer is None:
        return "cancel", None
    if isinstance(answer, tuple) and answer[0] is _DELETE:
        return "delete", answer[1]
    if isinstance(answer, tuple) and answer[0] is _SWEEP:
        return "sweep", answer[1]
    return "select", answer


def _keep_card_visible(question: "questionary.Question", cards: list[dict], width: int) -> None:
    """Scroll the whole selected card into view, not just its cursor line.

    The cursor sentinel sits on each card's middle row (see :func:`_render_cards`);
    setting the choices window's ``scroll_offsets`` to half the tallest card keeps
    that many lines visible above and below the cursor, so the full card — top
    border to bottom — stays on screen instead of running off the bottom. Purely
    cosmetic; any prompt_toolkit internals change is swallowed.
    """
    from prompt_toolkit.layout.containers import ScrollOffsets

    try:
        tallest = max((len(_card_lines(card, width)) for card in cards), default=1)
        pad = tallest // 2 + 1
        for container in question.application.layout.walk():
            content = getattr(container, "content", None)
            if type(content).__name__ == "InquirerControl":
                container.scroll_offsets = ScrollOffsets(top=pad, bottom=pad)
                return
    except Exception:  # noqa: BLE001 — cosmetic only, never block the prompt
        pass


def _wrap_choice_lines(question: "questionary.Question") -> None:
    """Let long choice rows wrap to the terminal width instead of being cut off.

    questionary's choices ``Window`` defaults to ``wrap_lines=False``, so a row
    wider than the terminal (a session's summary, especially on a narrow mobile
    terminal) is truncated at the right edge. We flip wrapping on for the window
    holding the choices; prompt_toolkit then wraps each over-long row. Purely
    cosmetic — any failure (a questionary/prompt_toolkit internals change) is
    swallowed, leaving the default truncation.
    """
    from prompt_toolkit.filters import to_filter

    try:
        for container in question.application.layout.walk():
            content = getattr(container, "content", None)
            if type(content).__name__ == "InquirerControl":
                container.wrap_lines = to_filter(True)
                return
    except Exception:  # noqa: BLE001 — cosmetic only, never block the prompt
        pass


def _pick_with_delete(message: str, choices: list) -> tuple[str, object]:
    """Show a ``select`` that also accepts ``x`` to delete the highlighted choice.

    Returns ``(action, value)`` where ``action`` is ``"select"`` (Enter on the
    highlighted choice), ``"delete"`` (``x`` pressed on it), or ``"cancel"``
    (``value`` is ``None``) when the user backed out. ``value`` is the chosen
    choice's value in the first two cases.

    The ``x`` shortcut is wired by reaching into the prompt's prompt_toolkit
    application — questionary's public ``select`` exposes no hook for extra
    keys — and reading the currently highlighted choice off its control.
    """
    from questionary.prompts.common import InquirerControl

    question = questionary.select(message, choices=choices)
    _wrap_choice_lines(question)
    control = next(
        c
        for c in question.application.layout.find_all_controls()
        if isinstance(c, InquirerControl)
    )

    @question.application.key_bindings.add("x", eager=True)
    def _request_delete(event) -> None:
        event.app.exit(result=(_DELETE, control.get_pointed_at().value))

    answer = question.ask()
    if answer is None:
        return "cancel", None
    if isinstance(answer, tuple) and answer[0] is _DELETE:
        return "delete", answer[1]
    return "select", answer


def _pick_repo(message: str, repos: list[str]) -> tuple[str, str | None]:
    """Show a repo picker that also accepts ``x`` to delete the highlighted repo.

    Returns ``(action, repo)`` where ``action`` is ``"select"`` (start a session
    from ``repo``), ``"delete"`` (remove ``repo`` from the workspaces dir), or
    ``"cancel"`` (``repo`` is ``None``) when the user backed out.
    """
    return _pick_with_delete(message, repos)


def _delete_repo(repo: str) -> None:
    """Delete a cloned repo and every worktree/session that belongs to it.

    Always confirms first; if the repo still has worktrees they are listed
    (flagged when running or holding unsaved work) so the loss is explicit.
    """
    workspace = config.workspaces_dir() / repo
    worktrees = [(name, path) for r, name, path in _list_worktrees() if r == repo]
    live = set(tmux_ops.list_sessions())

    if worktrees:
        plural = "" if len(worktrees) == 1 else "s"
        typer.secho(
            f"'{repo}' has {len(worktrees)} worktree{plural} that will also be deleted:",
            fg=typer.colors.YELLOW,
        )
        for name, path in worktrees:
            flags: list[str] = []
            if name in live:
                flags.append("running")
            try:
                if git_ops.is_dirty(path):
                    flags.append("uncommitted changes")
                unpushed = git_ops.unpushed_count(path)
                if unpushed:
                    flags.append(f"{unpushed} unpushed")
            except git_ops.GitError:
                pass
            suffix = f"  ({', '.join(flags)})" if flags else ""
            typer.secho(f"  - {name}{suffix}", fg=typer.colors.YELLOW)

    confirmed = questionary.confirm(
        f"Delete repo '{repo}' and all of its worktrees? This cannot be undone."
        if worktrees
        else f"Delete repo '{repo}'? This cannot be undone.",
        default=False,
    ).ask()
    if not confirmed:
        typer.secho("Cancelled — repo kept.", fg=typer.colors.CYAN)
        return

    # The worktree dirs are about to vanish; close any live sessions first.
    for name, _path in worktrees:
        if name in live:
            tmux_ops.kill_session(name)

    # Nuke both the worktrees and the clone wholesale — git's worktree metadata
    # lives inside the clone we're removing anyway, so no prune is needed.
    worktrees_root = config.worktrees_dir() / repo
    if worktrees_root.exists():
        shutil.rmtree(worktrees_root)
    shutil.rmtree(workspace)
    notes.forget_repo(repo)
    typer.secho(f"Deleted repo '{repo}'.", fg=typer.colors.GREEN)


def _menu_new_from_repo(default_agent: str, bypass: bool) -> None:
    """Pick an already-cloned repo and start a fresh worktree session.

    Pressing ``x`` on a highlighted repo deletes it (and all its worktrees)
    instead of starting a session.
    """
    repos = _list_repos()
    if not repos:
        typer.secho(
            "No repos cloned yet. Choose 'Add a new repo' instead.",
            fg=typer.colors.YELLOW,
        )
        return
    action, choice = _pick_repo(
        "New session from which repo?  ('x' deletes the highlighted repo)", repos
    )
    if action == "cancel":
        return
    if action == "delete":
        _delete_repo(choice)
        return
    agent = _pick_agent(default_agent)
    if agent is None:
        return
    _new_worktree_session(choice, config.workspaces_dir() / choice, agent, bypass)


# Sentinel choice in the repo picker: drop to a free-text clone-URL prompt
# instead of picking one of the listed GitHub repos.
_ENTER_URL = object()


def _cap_select_rows(question: "questionary.Question", rows: int) -> None:
    """Limit a ``questionary.select`` to ``rows`` visible choice rows.

    questionary renders the whole choice list inline, so a long list (hundreds
    of repos) would flood the terminal. We cap the height of the window holding
    the choices; prompt_toolkit then scrolls that window to follow the cursor.
    Purely cosmetic — any failure (a questionary/prompt_toolkit internals change)
    is swallowed, leaving the default full-height list.
    """
    from prompt_toolkit.layout.dimension import Dimension

    try:
        for container in question.application.layout.walk():
            content = getattr(container, "content", None)
            if type(content).__name__ == "InquirerControl":
                container.height = Dimension(min=1, max=rows)
                return
    except Exception:  # noqa: BLE001 — cosmetic only, never block the prompt
        pass


def _pick_github_repo(repos: list[str]) -> object | None:
    """Show a scrollable, filter-as-you-type list of GitHub repos.

    Returns the chosen ``owner/name`` string, the :data:`_ENTER_URL` sentinel
    when the user opts to type a clone URL instead, or ``None`` if cancelled.
    Typing filters the list by substring (questionary's ``use_search_filter``);
    at most 5 rows show at once, scrollable with the arrow keys.
    """
    choices = [
        questionary.Choice(title="↗  Enter a clone URL instead…", value=_ENTER_URL),
        *repos,
    ]
    question = questionary.select(
        "Pick a GitHub repo (type to filter), or enter a URL:",
        choices=choices,
        use_search_filter=True,  # typing filters the list (substring match)
        use_jk_keys=False,       # required with search filter: j/k become input
        show_selected=False,
    )
    _cap_select_rows(question, 5)
    return question.ask()


def _menu_add_repo(default_agent: str, bypass: bool) -> None:
    """Pick a GitHub repo (or enter a clone URL) and start a session from it.

    When ``gh`` is available and logged in, show a scrollable list of every repo
    the user can access (``owner/name``), filterable as you type. Picking one
    resolves to its clone URL; the "enter a clone URL" entry (and the whole flow
    when gh is unavailable) falls back to the original paste-a-URL behavior.
    """
    repos: list[str] = []
    if gh_ops.is_available():
        typer.secho("Fetching your GitHub repositories...", fg=typer.colors.CYAN)
        repos = gh_ops.list_repos()

    if repos:
        picked = _pick_github_repo(repos)
        if picked is None:
            return
        if picked is _ENTER_URL:
            url = (questionary.text("Git repository URL:").ask() or "").strip()
        else:
            url = gh_ops.clone_url(picked, config.configured_clone_protocol())  # type: ignore[arg-type]
    else:
        url = (questionary.text("Git repository URL:").ask() or "").strip()

    if not url:
        return
    agent = _pick_agent(default_agent)
    if agent is None:
        return
    _start_from_url(url, agent, bypass)


# Sentinel choice in the template picker: create the repo with no template.
_EMPTY_REPO = object()

#: GitHub's own rule for repository names — letters, digits, '.', '-', '_'.
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _pick_template(templates: list[str]) -> object | None:
    """Pick a template repo to generate a new project from, or an empty one.

    Same shape as :func:`_pick_github_repo` — scrollable, filter-as-you-type,
    5 rows — with the :data:`_EMPTY_REPO` sentinel first so "no template" is
    always one keystroke away even when the list is long. Returns the chosen
    ``owner/name``, the sentinel, or ``None`` if cancelled.
    """
    choices = [
        questionary.Choice(title="○  Empty repository (no template)", value=_EMPTY_REPO),
        *templates,
    ]
    question = questionary.select(
        "Start from which template (type to filter)?",
        choices=choices,
        use_search_filter=True,  # typing filters the list (substring match)
        use_jk_keys=False,       # required with search filter: j/k become input
        show_selected=False,
    )
    _cap_select_rows(question, 5)
    return question.ask()


def _pick_owner(owners: list[str]) -> str | None:
    """Pick the account to create the new repo under (personal login first)."""
    question = questionary.select(
        "Create it under which account?",
        choices=owners,
        use_search_filter=True,
        use_jk_keys=False,
        show_selected=False,
    )
    _cap_select_rows(question, 5)
    return question.ask()


def _prompt_repo_name(owner: str) -> str | None:
    """Ask for the new repository's name, validating it as you type.

    Rejecting the name in the prompt (rather than after the fact) keeps a typo
    from turning into a failed ``gh repo create`` round trip.
    """
    def valid(text: str) -> bool | str:
        text = text.strip()
        if not text:
            return "Enter a repository name."
        if not _REPO_NAME_RE.match(text):
            return "Use only letters, digits, '.', '-' and '_'."
        return True

    answer = questionary.text(f"New repository name ({owner}/…):", validate=valid).ask()
    return (answer or "").strip() or None


def _menu_new_github_project(default_agent: str, bypass: bool) -> None:
    """Create a brand-new GitHub repo (optionally from a template) and session it.

    Walks template → owner → name → visibility, creates the repo with ``gh``,
    then hands the clone URL to :func:`_start_from_url` so the result is exactly
    the same as picking an existing repo: cloned into the workspace, a worktree
    session created, agent attached.
    """
    if not gh_ops.is_available():
        typer.secho(
            "Creating a GitHub project needs the 'gh' CLI, installed and logged in "
            "(`gh auth login`).",
            fg=typer.colors.YELLOW,
        )
        return

    typer.secho("Fetching your GitHub templates...", fg=typer.colors.CYAN)
    picked = _pick_template(gh_ops.list_template_repos())
    if picked is None:
        return
    template = None if picked is _EMPTY_REPO else str(picked)

    owners = gh_ops.list_owners()
    if not owners:
        typer.secho(
            "Could not determine which accounts you can create repos under.",
            fg=typer.colors.YELLOW,
        )
        return
    owner = _pick_owner(owners)
    if owner is None:
        return

    name = _prompt_repo_name(owner)
    if name is None:
        return

    # gh has no default visibility in non-interactive mode, and guessing wrong
    # towards "public" is the one mistake here that can't be taken back.
    visibility = questionary.select(
        "Visibility:", choices=["private", "public"], default="private"
    ).ask()
    if visibility is None:
        return

    name_with_owner = f"{owner}/{name}"
    typer.secho(f"Creating {name_with_owner}...", fg=typer.colors.CYAN)
    gh_ops.create_repo(name_with_owner, template=template, private=visibility == "private")
    typer.secho(f"Created {name_with_owner}.", fg=typer.colors.GREEN)

    if template and not gh_ops.wait_for_commits(name_with_owner):
        typer.secho(
            "Timed out waiting for the template's contents to land — the clone "
            "may come up empty; re-run vv on it once GitHub catches up.",
            fg=typer.colors.YELLOW,
        )

    agent = _pick_agent(default_agent)
    if agent is None:
        return
    _start_from_url(
        gh_ops.clone_url(name_with_owner, config.configured_clone_protocol()),
        agent,
        bypass,
    )


def _menu_new_chat(default_agent: str, bypass: bool) -> None:
    """Start a fresh chat-only session (no git repo)."""
    agent = _pick_agent(default_agent)
    if agent is None:
        return
    _new_chat_session(agent, bypass)


def _banner() -> None:
    """Print vv's branch-diagram banner above the interactive menu."""
    dim = typer.colors.BRIGHT_BLACK
    typer.secho("●", fg=typer.colors.GREEN, bold=True, nl=False)
    typer.secho(" vv", fg=typer.colors.BRIGHT_WHITE, bold=True)
    typer.secho("│", fg=dim)
    typer.secho("╰─◍ ", fg=dim, nl=False)
    typer.secho("worktree sessions", fg=typer.colors.CYAN)
    typer.echo()


def _interactive_menu(default_agent: str, bypass: bool) -> None:
    """Top-level menu shown when vv is invoked with no arguments."""
    _banner()
    actions = {
        "●  List existing sessions": _menu_list_sessions,
        "➥  Start a new session from an existing repo": _menu_new_from_repo,
        "✚  Add a new repo": _menu_add_repo,
        "✦  Create a new GitHub project": _menu_new_github_project,
        "❝  Start a chat-only session (no repo)": _menu_new_chat,
    }
    choice = questionary.select("What would you like to do?", choices=list(actions)).ask()
    if choice is None:
        return
    actions[choice](default_agent, bypass)


@app.command()
def main(
    repo_url: str = typer.Argument(
        None,
        metavar="[REPO_URL]",
        help="Git repository URL. Omit to open the interactive menu.",
    ),
    agent: str = typer.Option(
        None,
        "--agent",
        "-a",
        metavar="COMMAND",
        envvar="VV_AGENT",
        help="Agent CLI to launch in the session. Falls back to the config "
        "file's `agent`, then 'claude'.",
    ),
    ask: bool | None = typer.Option(
        None,
        "--ask/--no-ask",
        help="Launch the agent with its normal permission prompts. vv "
        "bypasses them by default.",
    ),
    chat: bool = typer.Option(
        False,
        "--chat",
        "-c",
        help="Start a chat-only session (no git repo). Cannot be combined "
        "with a repo URL.",
    ),
    remote_mode: bool | None = typer.Option(
        None,
        "--remote/--local",
        envvar="VV_REMOTE",
        help="Force remote-launcher mode on/off, overriding the config's "
        "`mode`. Remote mode opens a cmux tab that runs vv on a server.",
    ),
    name: str = typer.Option(
        None,
        "--name",
        metavar="NAME",
        help="Use this exact session/worktree name instead of a random one. "
        "Forwarded by remote mode so the cmux tab mirrors the remote session. "
        "With --title/--label, names the session to annotate.",
    ),
    title: str = typer.Option(
        None,
        "--title",
        "-t",
        metavar="TEXT",
        help="Set the session's title, shown above its generated summary on the "
        "session card. Pass an empty string to clear it. On its own it titles "
        "the session you are in (or --name NAME); alongside a repo URL or "
        "--chat it titles the new session.",
    ),
    label: list[str] = typer.Option(
        None,
        "--label",
        "-l",
        metavar="TAG",
        help="Attach TAG to a session (repeatable). A leading '-' removes it "
        "(use --label=-TAG). On its own it labels the session you are in (or "
        "--name NAME); alongside a repo URL or --chat it labels the new session.",
    ),
    emit_cwd: str = typer.Option(
        None,
        "--emit-cwd",
        hidden=True,
        metavar="PATH",
        help="Internal: print a tmux-passthrough OSC 7 for PATH and exit. "
        "Invoked by vv's own cwd-forwarding tmux hook.",
    ),
) -> None:
    """Start (or rejoin) a worktree-backed agent session."""
    # Internal fast path for the cwd-forwarding tmux hook: emit and exit before
    # any config/mode resolution (it runs on every window switch).
    if emit_cwd is not None:
        tmux_ops.emit_cwd(Path(emit_cwd))
        return
    try:
        # Precedence: --agent flag / $VV_AGENT > config file > built-in default.
        # Typer fills `agent` from $VV_AGENT, with the explicit flag winning.
        resolved_agent = agent or config.configured_agent() or agents.DEFAULT_AGENT
        # Bypass permission prompts unless --ask (or the config) opts out;
        # an explicit --ask/--no-ask flag overrides the config setting.
        resolved_ask = ask if ask is not None else config.configured_ask()
        bypass = not resolved_ask

        # Remote-launcher mode: the explicit flag wins, else the config decides,
        # else local. When on, vv does no local work — it forwards to a remote.
        mode = (
            "remote" if remote_mode is True
            else "local" if remote_mode is False
            else config.configured_mode()
        )
        pending_notes = notes.Pending(title=title, label_specs=tuple(label or []))

        # Annotating an *existing* session is pure local bookkeeping on the
        # machine whose sessions they are, so it never routes through remote
        # mode: inside a remote session you are already running the remote vv.
        if pending_notes and not (repo_url or chat):
            _apply_notes(pending_notes, name)
            return

        if mode == "remote":
            if chat and repo_url:
                raise _fail("--chat cannot be combined with a repo URL")
            _launch_remote(repo_url, chat, agent, ask, name, pending_notes)
            return

        if chat:
            if repo_url:
                raise _fail("--chat cannot be combined with a repo URL")
            _new_chat_session(resolved_agent, bypass, name, pending_notes)
        elif repo_url:
            _start_from_url(repo_url, resolved_agent, bypass, name, pending_notes)
        else:
            _interactive_menu(resolved_agent, bypass)
    except (
        git_ops.GitError,
        tmux_ops.TmuxError,
        config.ConfigError,
        cmux_ops.CmuxError,
        gh_ops.GhError,
    ) as exc:
        raise _fail(str(exc)) from exc
    except KeyboardInterrupt:
        typer.secho("\nAborted.", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=130) from None


def run() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    run()
