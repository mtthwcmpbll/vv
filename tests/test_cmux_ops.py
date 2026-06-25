"""Tests for the cmux CLI wrappers.

These never touch a real cmux app: ``_run`` (or ``subprocess.run``) is stubbed
so the tests exercise argument construction and JSON parsing in isolation.
"""

from __future__ import annotations

import subprocess

import pytest

from vv import cmux_ops


def _completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["cmux"], returncode=returncode, stdout=stdout, stderr=""
    )


def _stub_run(monkeypatch, results):
    """Make ``cmux_ops._run`` return queued ``results`` and record its args."""
    queue = list(results)
    calls: list[list[str]] = []

    def fake(args, **kwargs):
        calls.append(args)
        return queue.pop(0) if queue else _completed()

    monkeypatch.setattr(cmux_ops, "_run", fake)
    return calls


# --- availability -----------------------------------------------------------

def test_is_available_reflects_path(monkeypatch):
    monkeypatch.setattr(cmux_ops.shutil, "which", lambda _c: "/usr/bin/cmux")
    assert cmux_ops.is_available() is True
    monkeypatch.setattr(cmux_ops.shutil, "which", lambda _c: None)
    assert cmux_ops.is_available() is False


def test_ensure_available_raises_when_missing(monkeypatch):
    monkeypatch.setattr(cmux_ops, "is_available", lambda: False)
    with pytest.raises(cmux_ops.CmuxError, match="not installed"):
        cmux_ops.ensure_available()


# --- list_workspace_titles --------------------------------------------------

def test_list_titles_parses_a_bare_list(monkeypatch):
    _stub_run(monkeypatch, [_completed(stdout='[{"title": "falcon"}, {"name": "otter"}]')])
    assert cmux_ops.list_workspace_titles() == ["falcon", "otter"]


def test_list_titles_parses_a_wrapped_object(monkeypatch):
    _stub_run(monkeypatch, [_completed(stdout='{"workspaces": [{"title": "raven"}]}')])
    assert cmux_ops.list_workspace_titles() == ["raven"]


def test_list_titles_degrades_to_empty_on_failure(monkeypatch):
    _stub_run(monkeypatch, [_completed(returncode=1)])
    assert cmux_ops.list_workspace_titles() == []


def test_list_titles_degrades_to_empty_on_bad_json(monkeypatch):
    _stub_run(monkeypatch, [_completed(stdout="not json")])
    assert cmux_ops.list_workspace_titles() == []


# --- new_workspace ----------------------------------------------------------

def test_new_workspace_creates_then_renames_by_id(monkeypatch, tmp_path):
    calls = _stub_run(monkeypatch, [_completed(stdout='{"id": "ws-123"}')])
    cmux_ops.new_workspace(tmp_path, "ssh -t h vv", title="falcon")
    assert calls[0] == [
        "new-workspace", "--cwd", str(tmp_path), "--command", "ssh -t h vv", "--json"
    ]
    assert calls[1] == ["rename-workspace", "--workspace", "ws-123", "falcon"]


def test_new_workspace_without_title_does_not_rename(monkeypatch, tmp_path):
    calls = _stub_run(monkeypatch, [_completed(stdout='{"id": "ws-1"}')])
    cmux_ops.new_workspace(tmp_path, "cmd")
    assert len(calls) == 1


def test_new_workspace_falls_back_to_bare_rename_without_id(monkeypatch, tmp_path):
    # new-workspace --json succeeds but yields no id; rename targets current.
    calls = _stub_run(monkeypatch, [_completed(stdout="{}")])
    cmux_ops.new_workspace(tmp_path, "cmd", title="otter")
    assert calls[1] == ["rename-workspace", "otter"]


def test_new_workspace_retries_without_json_flag(monkeypatch, tmp_path):
    # First call (with --json) fails; creation retried plainly, then rename.
    calls = _stub_run(monkeypatch, [_completed(returncode=1), _completed(), _completed()])
    cmux_ops.new_workspace(tmp_path, "cmd", title="raven")
    assert calls[0][-1] == "--json"
    assert calls[1] == ["new-workspace", "--cwd", str(tmp_path), "--command", "cmd"]
    assert calls[2] == ["rename-workspace", "raven"]


# --- _run error handling ----------------------------------------------------

def test_run_raises_when_cmux_is_missing(monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(cmux_ops.CmuxError, match="not installed"):
        cmux_ops._run(["list-workspaces"])


def test_run_raises_on_command_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "cmux", stderr="boom")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(cmux_ops.CmuxError, match="boom"):
        cmux_ops._run(["new-workspace"])
