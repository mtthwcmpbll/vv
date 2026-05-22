"""Thin wrappers around the ``git`` CLI for clones and worktrees."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    """Raised when a git command fails."""


def _run(args: list[str], *, capture: bool = False) -> str:
    """Run a git command, raising :class:`GitError` on failure."""
    try:
        result = subprocess.run(
            args,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except FileNotFoundError as exc:  # git not installed
        raise GitError("git is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or f"exit code {exc.returncode}"
        raise GitError(f"`{' '.join(args)}` failed: {detail}") from exc
    return (result.stdout or "").strip()


def repo_name_from_url(url: str) -> str:
    """Derive a directory-friendly repo name from a clone URL.

    Handles ``https://host/owner/name.git``, ``git@host:owner/name.git`` and
    trailing slashes.
    """
    name = url.rstrip("/").rsplit("/", 1)[-1]
    name = name.rsplit(":", 1)[-1]  # scp-style git@host:owner/name
    if name.endswith(".git"):
        name = name[:-4]
    if not name:
        raise GitError(f"could not derive a repo name from URL: {url!r}")
    return name


def clone(url: str, dest: Path) -> None:
    """Clone ``url`` into ``dest``."""
    _run(["git", "clone", url, str(dest)])


def fetch(workspace: Path) -> None:
    """Fetch all refs for the workspace clone (best effort)."""
    _run(["git", "-C", str(workspace), "fetch", "--all", "--prune", "--quiet"])


def default_start_ref(workspace: Path) -> str:
    """Return the best ref to branch new worktrees from.

    Prefers the remote's default branch (``origin/HEAD``); falls back to the
    local ``HEAD``.
    """
    try:
        ref = _run(
            ["git", "-C", str(workspace), "symbolic-ref", "refs/remotes/origin/HEAD"],
            capture=True,
        )
        if ref:
            return ref.removeprefix("refs/remotes/")
    except GitError:
        pass
    return "HEAD"


def add_worktree(workspace: Path, worktree_path: Path, branch: str, start_ref: str) -> None:
    """Create a new worktree with a fresh branch."""
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "git",
            "-C",
            str(workspace),
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree_path),
            start_ref,
        ]
    )


def list_worktrees(workspace: Path) -> list[Path]:
    """Return the paths of every worktree attached to this clone.

    The main working tree is included (typically first); callers that only
    want the disposable vv worktrees should filter it out by location.
    """
    out = _run(
        ["git", "-C", str(workspace), "worktree", "list", "--porcelain"],
        capture=True,
    )
    prefix = "worktree "
    return [
        Path(line[len(prefix) :])
        for line in out.splitlines()
        if line.startswith(prefix)
    ]


def existing_branches(workspace: Path) -> set[str]:
    """Return the set of local branch names in the workspace clone."""
    out = _run(
        ["git", "-C", str(workspace), "branch", "--format=%(refname:short)"],
        capture=True,
    )
    return {line.strip() for line in out.splitlines() if line.strip()}


def is_dirty(worktree_path: Path) -> bool:
    """Return True if the worktree has uncommitted changes.

    Covers staged, unstaged, and untracked files.
    """
    out = _run(
        ["git", "-C", str(worktree_path), "status", "--porcelain"],
        capture=True,
    )
    return bool(out)


def unpushed_count(worktree_path: Path) -> int:
    """Return the number of commits on HEAD that are on no remote.

    Counts commits reachable from HEAD but from no remote-tracking ref, so the
    result does not depend on the branch having an upstream configured.
    """
    out = _run(
        [
            "git", "-C", str(worktree_path),
            "rev-list", "--count", "HEAD", "--not", "--remotes",
        ],
        capture=True,
    )
    return int(out or "0")


def remove_worktree(workspace: Path, worktree_path: Path, *, force: bool = False) -> None:
    """Remove a worktree. With ``force``, discard any uncommitted changes."""
    args = ["git", "-C", str(workspace), "worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(worktree_path))
    _run(args)


def delete_branch(workspace: Path, branch: str, *, force: bool = False) -> None:
    """Delete a local branch. With ``force``, delete even if unmerged."""
    _run(["git", "-C", str(workspace), "branch", "-D" if force else "-d", branch])
