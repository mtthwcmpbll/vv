"""Shared fixtures for the vv test suite."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# Identity flags so commits work without depending on the host's git config.
_GIT_IDENTITY = (
    "-c", "user.email=tests@vv.invalid",
    "-c", "user.name=vv tests",
    "-c", "commit.gpgsign=false",
)


def _git(*args: str, cwd: Path) -> None:
    """Run a git command in ``cwd`` with a fixed identity."""
    subprocess.run(
        ["git", *_GIT_IDENTITY, *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@pytest.fixture
def git():
    """Run git with a fixed identity: ``git("commit", "-m", "x", cwd=path)``."""
    return _git


@pytest.fixture
def remote_repo(tmp_path: Path) -> Path:
    """A local git repo with a single commit on ``main`` and no remote.

    Usable both as a fake remote to clone from and, on its own, as a repo
    whose ``origin/HEAD`` is unset.
    """
    repo = tmp_path / "remote"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    return repo


@pytest.fixture
def empty_remote(tmp_path: Path) -> Path:
    """A bare git repo with no commits — a freshly-created, empty remote.

    Cloning it yields an unborn HEAD with nothing to branch from; being bare it
    can also be pushed to, mirroring a brand-new GitHub repo.
    """
    repo = tmp_path / "empty-remote.git"
    repo.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=repo)
    return repo
