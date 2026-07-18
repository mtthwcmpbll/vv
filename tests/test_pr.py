"""Tests for cached / background-refreshed pull-request status."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from vv import config, git_ops, pr


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Point the PR cache at tmp and stub git so fingerprints are controllable."""
    monkeypatch.setenv("WORKTREES_DIR", str(tmp_path / "wt"))

    fps: dict[Path, str | None] = {}

    def fake_branch(path):
        if fps.get(path) is None:
            raise git_ops.GitError("not a git repo")
        return "branch"

    def fake_head(path):
        fp = fps.get(path)
        if fp is None:
            raise git_ops.GitError("not a git repo")
        return fp

    monkeypatch.setattr(git_ops, "current_branch", fake_branch)
    monkeypatch.setattr(git_ops, "head_commit", fake_head)
    return fps


def _drain(snapshot, monkeypatch, fetch):
    """Run a snapshot's background refresh to completion; return {key: pr} results."""
    monkeypatch.setattr(pr.gh_ops, "pr_status", fetch)
    results: dict[object, object] = {}
    thread = snapshot.refresh(lambda key, pr_info: results.__setitem__(key, pr_info))
    thread.join(timeout=5)
    return results


def test_fingerprint_none_for_non_git(isolated, tmp_path):
    assert pr.session_fingerprint(tmp_path / "chat") is None


def test_snapshot_empty_cache_is_all_stale(isolated, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    isolated[a] = "sha-a"
    isolated[b] = "sha-b"
    snap = pr.Snapshot({("r", "a"): a, ("r", "b"): b})
    assert snap.cached == {}
    assert snap.stale_keys == {("r", "a"), ("r", "b")}


def test_refresh_fetches_calls_back_and_caches(isolated, monkeypatch, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    isolated[a] = "sha-a1"
    isolated[b] = "sha-b1"
    fetched = []

    def fetch(path):
        fetched.append(path)
        return {"number": 1, "state": "open", "checks": "passing"}

    results = _drain(pr.Snapshot({("r", "a"): a, ("r", "b"): b}), monkeypatch, fetch)
    assert set(results) == {("r", "a"), ("r", "b")}
    assert sorted(fetched) == sorted([a, b])

    # A second snapshot now sees the cached values and nothing stale.
    snap2 = pr.Snapshot({("r", "a"): a, ("r", "b"): b})
    assert set(snap2.cached) == {("r", "a"), ("r", "b")}
    assert snap2.stale_keys == set()

    # b's branch moves -> only b is stale again.
    isolated[b] = "sha-b2"
    snap3 = pr.Snapshot({("r", "a"): a, ("r", "b"): b})
    assert snap3.stale_keys == {("r", "b")}
    assert set(snap3.cached) == {("r", "a")}


def test_no_pr_answer_is_cached_and_not_refetched(isolated, monkeypatch, tmp_path):
    a = tmp_path / "a"
    isolated[a] = "sha-a1"
    calls = []

    def fetch(path):
        calls.append(path)
        return None  # no PR for this branch

    _drain(pr.Snapshot({("r", "a"): a}), monkeypatch, fetch)
    assert calls == [a]

    snap2 = pr.Snapshot({("r", "a"): a})
    assert snap2.cached == {}          # nothing to show
    assert snap2.stale_keys == set()   # but a "no PR" answer is remembered


def test_non_git_sessions_are_skipped(isolated, monkeypatch, tmp_path):
    chat = tmp_path / "chat"  # no fingerprint registered -> non-git
    snap = pr.Snapshot({("_chats", "chat"): chat})
    assert snap.cached == {} and snap.stale_keys == set()
    called = []
    _drain(snap, monkeypatch, lambda p: called.append(p))
    assert called == []


def test_refresh_prunes_deleted_sessions(isolated, monkeypatch, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    isolated[a] = "sha-a"
    isolated[b] = "sha-b"
    fetch = lambda p: {"number": 1, "state": "open", "checks": None}

    _drain(pr.Snapshot({("r", "a"): a, ("r", "b"): b}), monkeypatch, fetch)
    cached = json.loads(config.pr_cache_file().read_text())["sessions"]
    assert set(cached) == {"r/a", "r/b"}

    _drain(pr.Snapshot({("r", "a"): a}), monkeypatch, fetch)  # b gone
    cached = json.loads(config.pr_cache_file().read_text())["sessions"]
    assert set(cached) == {"r/a"}


def test_stop_suppresses_callbacks_but_still_caches(isolated, monkeypatch, tmp_path):
    a = tmp_path / "a"
    isolated[a] = "sha-a"
    monkeypatch.setattr(pr.gh_ops, "pr_status", lambda p: {"number": 1, "state": "open", "checks": None})

    stop = threading.Event()
    stop.set()  # already stopped before any result lands
    seen = []
    snap = pr.Snapshot({("r", "a"): a})
    snap.refresh(lambda key, pr_info: seen.append(key), stop=stop).join(timeout=5)

    assert seen == []  # no callbacks fired
    cached = json.loads(config.pr_cache_file().read_text())["sessions"]
    assert cached["r/a"]["pr"] == {"number": 1, "state": "open", "checks": None}  # still persisted
