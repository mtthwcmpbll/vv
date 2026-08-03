"""Tests for the gh (GitHub CLI) wrappers.

These never invoke real ``gh``: ``subprocess.run`` is stubbed so the tests
exercise argument construction, parsing, and the degrade-to-empty behavior in
isolation.
"""

from __future__ import annotations

import subprocess

import pytest

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
    out = "b/two\tfalse\na/one\tfalse\nb/two\tfalse\n\n"  # unsorted, a dupe, a blank
    calls = _stub_run(monkeypatch, _completed(stdout=out))
    assert gh_ops.list_repos() == ["a/one", "b/two"]
    # Spans org repos via the user/repos endpoint, paginated and cached.
    assert "user/repos" in calls[0]
    assert "--paginate" in calls[0]
    assert "--cache" in calls[0]


def test_list_repos_empty_on_nonzero_exit(monkeypatch):
    _stub_run(monkeypatch, _completed(returncode=1, stdout="should/ignore"))
    assert gh_ops.list_repos() == []


def test_list_repos_empty_when_gh_cannot_be_run(monkeypatch):
    _stub_run(monkeypatch, FileNotFoundError())
    assert gh_ops.list_repos() == []


def test_list_repos_detailed_carries_template_flag(monkeypatch):
    out = "a/one\ttrue\nb/two\tfalse\n"
    _stub_run(monkeypatch, _completed(stdout=out))
    assert gh_ops.list_repos_detailed() == [("a/one", True), ("b/two", False)]


def test_list_repos_detailed_defaults_missing_flag_to_false(monkeypatch):
    """A row without the tab-separated flag is a plain repo, not a crash."""
    _stub_run(monkeypatch, _completed(stdout="a/one\n"))
    assert gh_ops.list_repos_detailed() == [("a/one", False)]


# --- list_template_repos ----------------------------------------------------

def test_list_template_repos_filters_to_templates(monkeypatch):
    out = "a/one\ttrue\nb/two\tfalse\nc/three\tTRUE\n"
    calls = _stub_run(monkeypatch, _completed(stdout=out))
    assert gh_ops.list_template_repos() == ["a/one", "c/three"]
    # Same cached user/repos walk as list_repos -- no second endpoint.
    assert len(calls) == 1
    assert "user/repos" in calls[0]


def test_list_template_repos_empty_when_none_marked(monkeypatch):
    _stub_run(monkeypatch, _completed(stdout="a/one\tfalse\n"))
    assert gh_ops.list_template_repos() == []


def test_list_template_repos_empty_on_failure(monkeypatch):
    _stub_run(monkeypatch, _completed(returncode=1, stdout="a/one\ttrue"))
    assert gh_ops.list_template_repos() == []


# --- list_owners ------------------------------------------------------------

def _stub_sequence(monkeypatch, results):
    """Return successive ``results`` from subprocess.run, recording argv."""
    calls: list[list[str]] = []
    queue = list(results)

    def fake(args, **kwargs):
        calls.append(args)
        item = queue.pop(0) if queue else _completed()
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(gh_ops.subprocess, "run", fake)
    return calls


def test_list_owners_puts_login_first_then_sorted_orgs(monkeypatch):
    calls = _stub_sequence(
        monkeypatch, [_completed(stdout="octocat\n"), _completed(stdout="zeta\nacme\n")]
    )
    assert gh_ops.list_owners() == ["octocat", "acme", "zeta"]
    assert "user" in calls[0]
    assert "user/orgs" in calls[1]
    # gh flips to POST as soon as a -f parameter is present; the orgs read
    # passes per_page, so it must pin the method or it 404s.
    assert calls[1][calls[1].index("-X") + 1] == "GET"


def test_list_owners_drops_org_duplicating_login(monkeypatch):
    _stub_sequence(
        monkeypatch, [_completed(stdout="octocat\n"), _completed(stdout="octocat\n")]
    )
    assert gh_ops.list_owners() == ["octocat"]


def test_list_owners_survives_org_lookup_failure(monkeypatch):
    """No org access is normal -- the personal account is still offered."""
    _stub_sequence(monkeypatch, [_completed(stdout="octocat\n"), _completed(returncode=1)])
    assert gh_ops.list_owners() == ["octocat"]


def test_list_owners_empty_when_gh_fails(monkeypatch):
    _stub_sequence(monkeypatch, [_completed(returncode=1), _completed(returncode=1)])
    assert gh_ops.list_owners() == []


# --- create_repo ------------------------------------------------------------

def test_create_repo_builds_private_command(monkeypatch):
    calls = _stub_run(monkeypatch, _completed())
    gh_ops.create_repo("octo/thing", private=True)
    assert calls[0] == ["gh", "repo", "create", "octo/thing", "--private"]


def test_create_repo_passes_template_and_public(monkeypatch):
    calls = _stub_run(monkeypatch, _completed())
    gh_ops.create_repo("octo/thing", template="octo/tpl", private=False)
    assert calls[0] == [
        "gh", "repo", "create", "octo/thing",
        "--template", "octo/tpl",
        "--public",
    ]


def test_create_repo_raises_with_gh_stderr(monkeypatch):
    result = subprocess.CompletedProcess(
        args=["gh"], returncode=1, stdout="", stderr="Name already exists"
    )
    _stub_run(monkeypatch, result)
    with pytest.raises(gh_ops.GhError, match="Name already exists"):
        gh_ops.create_repo("octo/thing")


def test_create_repo_raises_when_gh_missing(monkeypatch):
    _stub_run(monkeypatch, FileNotFoundError("no gh"))
    with pytest.raises(gh_ops.GhError):
        gh_ops.create_repo("octo/thing")


# --- has_commits / wait_for_commits -----------------------------------------

def test_has_commits_true_when_endpoint_answers(monkeypatch):
    calls = _stub_run(monkeypatch, _completed(stdout="1\n"))
    assert gh_ops.has_commits("octo/thing") is True
    assert "repos/octo/thing/commits" in calls[0]
    assert "--cache" not in calls[0]  # polling a cached answer would never move
    # Same -f-implies-POST trap as list_owners: without -X GET this 404s.
    assert calls[0][calls[0].index("-X") + 1] == "GET"


def test_has_commits_false_on_empty_repo(monkeypatch):
    """GitHub answers 409 for an empty repo, which gh exits non-zero on."""
    _stub_run(monkeypatch, _completed(returncode=1))
    assert gh_ops.has_commits("octo/thing") is False


def test_wait_for_commits_returns_once_content_lands(monkeypatch):
    calls = _stub_sequence(
        monkeypatch,
        [_completed(returncode=1), _completed(returncode=1), _completed(stdout="1\n")],
    )
    slept: list[float] = []
    monkeypatch.setattr(gh_ops.time, "sleep", slept.append)
    assert gh_ops.wait_for_commits("octo/thing", timeout=10, interval=0.5) is True
    assert len(calls) == 3
    assert slept == [0.5, 0.5]  # slept only between polls, not after the hit


def test_wait_for_commits_gives_up_at_timeout(monkeypatch):
    _stub_run(monkeypatch, _completed(returncode=1))
    monkeypatch.setattr(gh_ops.time, "sleep", lambda _s: None)
    ticks = iter([0.0, 5.0, 10.0, 15.0])
    monkeypatch.setattr(gh_ops.time, "monotonic", lambda: next(ticks))
    assert gh_ops.wait_for_commits("octo/thing", timeout=10, interval=0.5) is False


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
