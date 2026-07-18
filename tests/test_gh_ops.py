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


# --- pr_status --------------------------------------------------------------

import json
from pathlib import Path


def _pr_json(number, state, checks, is_draft=False):
    rollup = {
        "passing": [{"conclusion": "SUCCESS"}],
        "failing": [{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}],
        "pending": [{"status": "IN_PROGRESS"}],
        None: [],
    }[checks]
    return json.dumps(
        {"number": number, "state": state, "isDraft": is_draft, "statusCheckRollup": rollup}
    )


def test_pr_status_parses_open_failing(monkeypatch):
    _stub_run(monkeypatch, _completed(0, _pr_json(3, "OPEN", "failing")))
    assert gh_ops.pr_status(Path("/wt")) == {"number": 3, "state": "open", "checks": "failing"}


def test_pr_status_parses_passing_and_pending(monkeypatch):
    _stub_run(monkeypatch, _completed(0, _pr_json(5, "OPEN", "passing")))
    assert gh_ops.pr_status(Path("/wt"))["checks"] == "passing"
    _stub_run(monkeypatch, _completed(0, _pr_json(6, "OPEN", "pending")))
    assert gh_ops.pr_status(Path("/wt"))["checks"] == "pending"


def test_pr_status_no_checks_is_none(monkeypatch):
    _stub_run(monkeypatch, _completed(0, _pr_json(7, "MERGED", None)))
    assert gh_ops.pr_status(Path("/wt")) == {"number": 7, "state": "merged", "checks": None}


def test_pr_status_promotes_draft(monkeypatch):
    _stub_run(monkeypatch, _completed(0, _pr_json(9, "OPEN", None, is_draft=True)))
    assert gh_ops.pr_status(Path("/wt"))["state"] == "draft"


def test_pr_status_open_not_draft(monkeypatch):
    _stub_run(monkeypatch, _completed(0, _pr_json(9, "OPEN", "passing", is_draft=False)))
    assert gh_ops.pr_status(Path("/wt"))["state"] == "open"


def test_pr_status_none_when_no_pr(monkeypatch):
    _stub_run(monkeypatch, _completed(returncode=1))  # gh: no pull requests found
    assert gh_ops.pr_status(Path("/wt")) is None


def test_pr_status_none_when_gh_missing(monkeypatch):
    _stub_run(monkeypatch, FileNotFoundError())
    assert gh_ops.pr_status(Path("/wt")) is None


def test_pr_status_none_on_timeout(monkeypatch):
    _stub_run(monkeypatch, subprocess.TimeoutExpired(["gh"], 6))
    assert gh_ops.pr_status(Path("/wt")) is None
