"""Tests for the gh (GitHub CLI) wrappers.

These never invoke real ``gh``: ``subprocess.run`` is stubbed so the tests
exercise argument construction, parsing, and the degrade-to-empty behavior in
isolation.
"""

from __future__ import annotations

import subprocess

from vv import gh_ops


def _completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=""
    )


def _stub_run(monkeypatch, result):
    """Make ``gh_ops``'s subprocess.run return ``result`` and record argv."""
    calls: list[list[str]] = []

    def fake(args, **kwargs):
        calls.append(args)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(gh_ops.subprocess, "run", fake)
    return calls


# --- is_available -----------------------------------------------------------

def test_is_available_false_when_gh_missing(monkeypatch):
    monkeypatch.setattr(gh_ops.shutil, "which", lambda _c: None)
    assert gh_ops.is_available() is False


def test_is_available_true_when_authenticated(monkeypatch):
    monkeypatch.setattr(gh_ops.shutil, "which", lambda _c: "/usr/bin/gh")
    _stub_run(monkeypatch, _completed(returncode=0))
    assert gh_ops.is_available() is True


def test_is_available_false_when_logged_out(monkeypatch):
    monkeypatch.setattr(gh_ops.shutil, "which", lambda _c: "/usr/bin/gh")
    _stub_run(monkeypatch, _completed(returncode=1))
    assert gh_ops.is_available() is False


# --- list_repos -------------------------------------------------------------

def test_list_repos_parses_sorts_and_dedupes(monkeypatch):
    out = "b/two\na/one\nb/two\n\n"  # unsorted, a dupe, and a blank line
    calls = _stub_run(monkeypatch, _completed(stdout=out))
    assert gh_ops.list_repos() == ["a/one", "b/two"]
    # Spans org repos via the user/repos endpoint, paginated.
    assert "user/repos" in calls[0]
    assert "--paginate" in calls[0]


def test_list_repos_empty_on_nonzero_exit(monkeypatch):
    _stub_run(monkeypatch, _completed(returncode=1, stdout="should/ignore"))
    assert gh_ops.list_repos() == []


def test_list_repos_empty_when_gh_cannot_be_run(monkeypatch):
    _stub_run(monkeypatch, FileNotFoundError())
    assert gh_ops.list_repos() == []


# --- clone_url --------------------------------------------------------------

def test_clone_url_defaults_to_ssh():
    assert gh_ops.clone_url("octo/repo") == "git@github.com:octo/repo.git"


def test_clone_url_ssh_explicit():
    assert gh_ops.clone_url("octo/repo", "ssh") == "git@github.com:octo/repo.git"


def test_clone_url_https():
    assert gh_ops.clone_url("octo/repo", "https") == "https://github.com/octo/repo.git"
