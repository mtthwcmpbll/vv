"""Tests for the git CLI wrappers.

URL parsing is pure; the worktree/branch helpers run real ``git`` against
throwaway repositories created by the ``remote_repo`` fixture.
"""

from __future__ import annotations

import subprocess

import pytest

from vv import git_ops


# --- repo_name_from_url (pure) ----------------------------------------------

@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/owner/repo.git", "repo"),
        ("https://github.com/owner/repo", "repo"),
        ("git@github.com:owner/repo.git", "repo"),
        ("https://github.com/owner/repo/", "repo"),
        ("git@github.com:owner/repo.git/", "repo"),
        ("ssh://git@host:22/owner/repo.git", "repo"),
        ("/local/path/to/myrepo", "myrepo"),
    ],
)
def test_repo_name_from_url(url, expected):
    assert git_ops.repo_name_from_url(url) == expected


@pytest.mark.parametrize("bad", ["", "/", "///"])
def test_repo_name_from_url_rejects_nameless_urls(bad):
    with pytest.raises(git_ops.GitError):
        git_ops.repo_name_from_url(bad)


# --- _run error handling ----------------------------------------------------

def test_run_raises_git_error_when_git_is_missing(monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(git_ops.GitError, match="not installed"):
        git_ops._run(["git", "status"])


# --- real-git operations ----------------------------------------------------

def test_clone_creates_a_working_copy(remote_repo, tmp_path):
    dest = tmp_path / "clone"
    git_ops.clone(str(remote_repo), dest)
    assert (dest / ".git").exists()
    assert (dest / "README.md").read_text() == "hello\n"


def test_clone_failure_raises_git_error(tmp_path):
    with pytest.raises(git_ops.GitError):
        git_ops.clone(str(tmp_path / "does-not-exist"), tmp_path / "dest")


def test_default_start_ref_uses_origin_head_after_clone(remote_repo, tmp_path):
    clone = tmp_path / "clone"
    git_ops.clone(str(remote_repo), clone)
    assert git_ops.default_start_ref(clone) == "origin/main"


def test_default_start_ref_falls_back_to_head_without_origin(remote_repo):
    # remote_repo has commits but no remote, so origin/HEAD is unresolvable.
    assert git_ops.default_start_ref(remote_repo) == "HEAD"


def test_has_head_commit_true_after_clone(remote_repo, tmp_path):
    clone = tmp_path / "clone"
    git_ops.clone(str(remote_repo), clone)
    assert git_ops.has_head_commit(clone) is True


def test_has_head_commit_false_for_empty_repo(empty_remote, tmp_path):
    # A freshly-created remote with no commits clones to an unborn HEAD.
    clone = tmp_path / "clone"
    git_ops.clone(str(empty_remote), clone)
    assert git_ops.has_head_commit(clone) is False


def test_seed_initial_commit_and_push_bootstraps_empty_repo(empty_remote, tmp_path):
    clone = tmp_path / "clone"
    git_ops.clone(str(empty_remote), clone)

    git_ops.seed_initial_commit(clone)
    git_ops.push_current(clone)

    # The clone now has a commit on its default branch...
    assert git_ops.has_head_commit(clone) is True
    assert "main" in git_ops.existing_branches(clone)
    # ...and it was pushed to the remote (so the worktree base is shared).
    remote_branches = subprocess.run(
        ["git", "-C", str(empty_remote), "branch", "--format=%(refname:short)"],
        check=True, text=True, capture_output=True,
    ).stdout.split()
    assert "main" in remote_branches

    # A worktree branches off the seeded default branch like any normal repo.
    worktree = tmp_path / "wt" / "falcon"
    start_ref = git_ops.default_start_ref(clone)
    git_ops.add_worktree(clone, worktree, branch="falcon", start_ref=start_ref)
    assert worktree.is_dir()
    assert "falcon" in git_ops.existing_branches(clone)
    # The seed commit is already on the remote, so nothing here is unpushed.
    assert git_ops.unpushed_count(worktree) == 0


def test_fetch_succeeds_on_a_clone(remote_repo, tmp_path):
    clone = tmp_path / "clone"
    git_ops.clone(str(remote_repo), clone)
    git_ops.fetch(clone)  # must not raise


def test_existing_branches_lists_local_branches(remote_repo):
    assert git_ops.existing_branches(remote_repo) == {"main"}


def test_add_worktree_creates_branch_and_checkout(remote_repo, tmp_path):
    clone = tmp_path / "clone"
    git_ops.clone(str(remote_repo), clone)

    worktree = tmp_path / "wt" / "falcon"
    git_ops.add_worktree(clone, worktree, branch="falcon", start_ref="origin/main")

    assert (worktree / "README.md").exists()
    assert "falcon" in git_ops.existing_branches(clone)


def test_list_worktrees_includes_main_and_added_worktrees(remote_repo, tmp_path):
    clone = tmp_path / "clone"
    git_ops.clone(str(remote_repo), clone)
    worktree = tmp_path / "wt" / "otter"
    git_ops.add_worktree(clone, worktree, branch="otter", start_ref="origin/main")

    found = {p.resolve() for p in git_ops.list_worktrees(clone)}
    assert clone.resolve() in found
    assert worktree.resolve() in found


# --- worktree state + cleanup -----------------------------------------------

@pytest.fixture
def worktree(remote_repo, tmp_path):
    """A clone with a single worktree on branch ``falcon``; yields (clone, wt)."""
    clone = tmp_path / "clone"
    git_ops.clone(str(remote_repo), clone)
    wt = tmp_path / "wt" / "falcon"
    git_ops.add_worktree(clone, wt, branch="falcon", start_ref="origin/main")
    return clone, wt


def test_is_dirty_false_on_a_clean_worktree(worktree):
    _, wt = worktree
    assert git_ops.is_dirty(wt) is False


def test_is_dirty_true_with_modified_file(worktree):
    _, wt = worktree
    (wt / "README.md").write_text("changed\n")
    assert git_ops.is_dirty(wt) is True


def test_is_dirty_true_with_untracked_file(worktree):
    _, wt = worktree
    (wt / "scratch.txt").write_text("note\n")
    assert git_ops.is_dirty(wt) is True


def test_unpushed_count_zero_on_a_fresh_worktree(worktree):
    _, wt = worktree
    assert git_ops.unpushed_count(wt) == 0


def test_unpushed_count_counts_local_commits(worktree, git):
    _, wt = worktree
    for n in (1, 2):
        (wt / f"file{n}.txt").write_text("x\n")
        git("add", "-A", cwd=wt)
        git("commit", "-q", "-m", f"work {n}", cwd=wt)
    assert git_ops.unpushed_count(wt) == 2


def test_remove_worktree_deletes_a_clean_worktree(worktree):
    clone, wt = worktree
    git_ops.remove_worktree(clone, wt)
    assert not wt.exists()
    assert wt.resolve() not in {p.resolve() for p in git_ops.list_worktrees(clone)}


def test_remove_worktree_needs_force_when_dirty(worktree):
    clone, wt = worktree
    (wt / "README.md").write_text("dirty\n")
    with pytest.raises(git_ops.GitError):
        git_ops.remove_worktree(clone, wt)
    git_ops.remove_worktree(clone, wt, force=True)
    assert not wt.exists()


def test_delete_branch_removes_a_merged_branch(worktree):
    clone, wt = worktree
    git_ops.remove_worktree(clone, wt, force=True)
    assert "falcon" in git_ops.existing_branches(clone)
    git_ops.delete_branch(clone, "falcon")
    assert "falcon" not in git_ops.existing_branches(clone)


def test_delete_branch_needs_force_when_unmerged(worktree, git):
    clone, wt = worktree
    (wt / "file.txt").write_text("x\n")
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", "unmerged work", cwd=wt)
    git_ops.remove_worktree(clone, wt, force=True)

    with pytest.raises(git_ops.GitError):
        git_ops.delete_branch(clone, "falcon")
    git_ops.delete_branch(clone, "falcon", force=True)
    assert "falcon" not in git_ops.existing_branches(clone)
