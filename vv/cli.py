"""Command-line entry point for vv."""

from __future__ import annotations

import shutil
from pathlib import Path

import questionary
import typer

from . import agents, config, git_ops, names, tmux_ops

app = typer.Typer(
    add_completion=False,
    help="Spin up disposable git worktree + tmux + agent CLI sessions.",
)

# Sentinel "repo" identifier for chat-only sessions: they live under
# WORKTREES_DIR/_chats/<name> instead of belonging to a real cloned repo.
CHATS = "_chats"


def _fail(message: str) -> "typer.Exit":
    """Print an error and return an Exit to raise."""
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(code=1)


def _list_repos() -> list[str]:
    """Return repo names that have been cloned into the workspaces dir."""
    root = config.workspaces_dir()
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def _list_worktrees() -> list[tuple[str, str, Path]]:
    """Return ``(repo, name, path)`` for every vv session across all repos.

    Chat-only sessions (no git worktree) are surfaced under the sentinel
    :data:`CHATS` namespace so the same menu can resume / delete them.
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
    return sorted(found)


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


def _resume_worktree(name: str, worktree_path: Path, agent: str) -> None:
    """Attach to the worktree's tmux session, creating it fresh if none is live.

    The worktree is the session: if vv already has a tmux session of this name
    we hand the terminal to it; otherwise we start one rooted at the worktree
    and launch ``agent``, just like a brand-new session but with existing state.
    """
    if tmux_ops.session_exists(name):
        typer.secho(f"Joining live session '{name}'...", fg=typer.colors.CYAN)
    else:
        typer.secho(
            f"Starting tmux session '{name}' and launching {agent}...",
            fg=typer.colors.CYAN,
        )
        if not agents.is_installed(agent):
            typer.secho(
                f"  warning: '{agent}' was not found on PATH", fg=typer.colors.YELLOW
            )
        tmux_ops.create_session(name, worktree_path)
        tmux_ops.send_command(name, agent)

    typer.secho(f"  worktree: {worktree_path}", fg=typer.colors.GREEN)
    typer.secho(f"  session:  {name}", fg=typer.colors.GREEN)
    tmux_ops.attach(name)


def _new_worktree_session(repo_name: str, workspace: Path, agent: str) -> None:
    """Create a worktree + tmux session for an already-cloned repo and attach."""
    worktree_root = config.worktrees_dir() / repo_name

    taken: set[str] = set(tmux_ops.list_sessions())
    taken |= git_ops.existing_branches(workspace)
    if worktree_root.exists():
        taken |= {p.name for p in worktree_root.iterdir()}

    name = names.random_name(taken)
    worktree_path = worktree_root / name

    start_ref = git_ops.default_start_ref(workspace)
    typer.secho(
        f"Creating worktree '{name}' (branch off {start_ref})...",
        fg=typer.colors.CYAN,
    )
    git_ops.add_worktree(workspace, worktree_path, branch=name, start_ref=start_ref)

    _resume_worktree(name, worktree_path, agent)


def _new_chat_session(agent: str) -> None:
    """Create an empty chat-only session dir and attach an agent to it.

    Chat sessions are not backed by a git worktree — they are just a plain
    directory under :func:`config.chats_dir`, intended for persistent agent
    conversations that don't need (or want) version control.
    """
    chats_root = config.chats_dir()

    # Avoid colliding with any existing tmux session or vv session name.
    taken: set[str] = set(tmux_ops.list_sessions())
    taken |= {name for _repo, name, _path in _list_worktrees()}

    name = names.random_name(taken)
    chat_path = chats_root / name
    chat_path.mkdir(parents=True)

    typer.secho(f"Creating chat session '{name}'...", fg=typer.colors.CYAN)
    _resume_worktree(name, chat_path, agent)


def _start_from_url(repo_url: str, agent: str) -> None:
    """Clone the repo if needed, then create a new worktree session."""
    repo_name = git_ops.repo_name_from_url(repo_url)
    workspace = config.workspaces_dir() / repo_name

    if workspace.exists():
        typer.secho(f"Repo '{repo_name}' already cloned, fetching latest...", fg=typer.colors.CYAN)
        try:
            git_ops.fetch(workspace)
        except git_ops.GitError as exc:
            typer.secho(f"  (fetch failed, continuing: {exc})", fg=typer.colors.YELLOW)
    else:
        typer.secho(f"Cloning '{repo_name}'...", fg=typer.colors.CYAN)
        git_ops.clone(repo_url, workspace)

    _new_worktree_session(repo_name, workspace, agent)


def _resume_session(name: str, path: Path, default_agent: str, live: set[str]) -> None:
    """Resume a worktree's session, picking an agent if it must be restarted."""
    # A live session is just re-attached; only a dead one needs an agent, so
    # ask which CLI to relaunch it with (vv does not track the prior choice).
    if name in live:
        agent = default_agent
    else:
        agent = _pick_agent(default_agent)
        if agent is None:
            return
    _resume_worktree(name, path, agent)


def _delete_chat(name: str, path: Path, live: set[str]) -> None:
    """Delete a chat session dir, warning first if it has any contents."""
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
            return

    if name in live:
        tmux_ops.kill_session(name)
    shutil.rmtree(path)
    typer.secho(f"Deleted chat '{name}'.", fg=typer.colors.GREEN)


def _delete_session(repo: str, name: str, path: Path, live: set[str]) -> None:
    """Delete a session, warning first if it holds work that would be lost."""
    if repo == CHATS:
        return _delete_chat(name, path, live)

    workspace = config.workspaces_dir() / repo

    risks: list[str] = []
    if git_ops.is_dirty(path):
        risks.append("uncommitted changes in the working tree")
    unpushed = git_ops.unpushed_count(path)
    if unpushed:
        plural = "" if unpushed == 1 else "s"
        risks.append(f"{unpushed} commit{plural} not pushed to any remote")

    if risks:
        typer.secho(f"'{repo}/{name}' has work that would be lost:", fg=typer.colors.YELLOW)
        for risk in risks:
            typer.secho(f"  - {risk}", fg=typer.colors.YELLOW)
        confirmed = questionary.confirm(
            "Delete it anyway? This cannot be undone.", default=False
        ).ask()
        if not confirmed:
            typer.secho("Cancelled — worktree kept.", fg=typer.colors.CYAN)
            return

    # The session's working directory is about to vanish; close it first.
    if name in live:
        tmux_ops.kill_session(name)
    git_ops.remove_worktree(workspace, path, force=True)
    git_ops.delete_branch(workspace, name, force=True)
    typer.secho(f"Deleted worktree '{repo}/{name}'.", fg=typer.colors.GREEN)


def _menu_list_sessions(default_agent: str) -> None:
    """List existing worktrees; resume or delete the chosen one."""
    worktrees = _list_worktrees()
    if not worktrees:
        typer.secho(
            "No worktrees yet. Choose a repo to start one.",
            fg=typer.colors.YELLOW,
        )
        return
    live = set(tmux_ops.list_sessions())
    choices: dict[str, tuple[str, str, Path]] = {}
    for repo, name, path in worktrees:
        glyph = "●" if name in live else "○"  # running / idle
        choices[f"{glyph}  {repo}/{name}"] = (repo, name, path)
    choice = questionary.select(
        "Which session?  (● running  ○ idle)", choices=list(choices)
    ).ask()
    if choice is None:
        return
    repo, name, path = choices[choice]

    action = questionary.select(
        f"{repo}/{name} — what would you like to do?",
        choices=[
            questionary.Choice(title="↻  Resume", value="resume"),
            questionary.Choice(title="✕  Delete", value="delete"),
        ],
    ).ask()
    if action == "resume":
        _resume_session(name, path, default_agent, live)
    elif action == "delete":
        _delete_session(repo, name, path, live)


def _menu_new_from_repo(default_agent: str) -> None:
    """Pick an already-cloned repo and start a fresh worktree session."""
    repos = _list_repos()
    if not repos:
        typer.secho(
            "No repos cloned yet. Choose 'Add a new repo' instead.",
            fg=typer.colors.YELLOW,
        )
        return
    choice = questionary.select("New session from which repo?", choices=repos).ask()
    if choice is None:
        return
    agent = _pick_agent(default_agent)
    if agent is None:
        return
    _new_worktree_session(choice, config.workspaces_dir() / choice, agent)


def _menu_add_repo(default_agent: str) -> None:
    """Prompt for a clone URL and start a session from it."""
    url = questionary.text("Git repository URL:").ask()
    if not url:
        return
    agent = _pick_agent(default_agent)
    if agent is None:
        return
    _start_from_url(url.strip(), agent)


def _menu_new_chat(default_agent: str) -> None:
    """Start a fresh chat-only session (no git repo)."""
    agent = _pick_agent(default_agent)
    if agent is None:
        return
    _new_chat_session(agent)


def _banner() -> None:
    """Print vv's branch-diagram banner above the interactive menu."""
    dim = typer.colors.BRIGHT_BLACK
    typer.secho("●", fg=typer.colors.GREEN, bold=True, nl=False)
    typer.secho(" vv", fg=typer.colors.BRIGHT_WHITE, bold=True)
    typer.secho("│", fg=dim)
    typer.secho("╰─◍ ", fg=dim, nl=False)
    typer.secho("worktree sessions", fg=typer.colors.CYAN)
    typer.echo()


def _interactive_menu(default_agent: str) -> None:
    """Top-level menu shown when vv is invoked with no arguments."""
    _banner()
    actions = {
        "⊞  List existing sessions": _menu_list_sessions,
        "✦  Start a new session from an existing repo": _menu_new_from_repo,
        "⊕  Add a new repo": _menu_add_repo,
        "✎  Start a chat-only session (no repo)": _menu_new_chat,
    }
    choice = questionary.select("What would you like to do?", choices=list(actions)).ask()
    if choice is None:
        return
    actions[choice](default_agent)


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
    chat: bool = typer.Option(
        False,
        "--chat",
        "-c",
        help="Start a chat-only session (no git repo). Cannot be combined "
        "with a repo URL.",
    ),
) -> None:
    """Start (or rejoin) a worktree-backed agent session."""
    try:
        # Precedence: --agent flag / $VV_AGENT > config file > built-in default.
        # Typer fills `agent` from $VV_AGENT, with the explicit flag winning.
        resolved_agent = agent or config.configured_agent() or agents.DEFAULT_AGENT
        if chat:
            if repo_url:
                raise _fail("--chat cannot be combined with a repo URL")
            _new_chat_session(resolved_agent)
        elif repo_url:
            _start_from_url(repo_url, resolved_agent)
        else:
            _interactive_menu(resolved_agent)
    except (git_ops.GitError, tmux_ops.TmuxError, config.ConfigError) as exc:
        raise _fail(str(exc)) from exc
    except KeyboardInterrupt:
        typer.secho("\nAborted.", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=130) from None


def run() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    run()
