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


# --- new_ssh_workspace ------------------------------------------------------

def test_new_ssh_workspace_builds_args_and_returns_id(monkeypatch):
    calls = _stub_run(monkeypatch, [_completed(stdout='{"workspace_id": "ws-123"}')])
    ws_id = cmux_ops.new_ssh_workspace(
        "matt@box",
        name="falcon",
        port=2222,
        identity="~/.ssh/id_ed25519",
        ssh_options=("StrictHostKeyChecking=no",),
    )
    assert ws_id == "ws-123"
    assert calls[0] == [
        "ssh", "matt@box", "--json",
        "--name", "falcon",
        "--port", "2222",
        "--identity", "~/.ssh/id_ed25519",
        "--ssh-option", "StrictHostKeyChecking=no",
    ]


def test_new_ssh_workspace_omits_unset_flags(monkeypatch):
    calls = _stub_run(monkeypatch, [_completed(stdout='{"workspace_ref": "workspace:1"}')])
    ws_id = cmux_ops.new_ssh_workspace("host")
    assert ws_id == "workspace:1"
    assert calls[0] == ["ssh", "host", "--json"]


def test_new_ssh_workspace_returns_none_on_unparseable_payload(monkeypatch):
    _stub_run(monkeypatch, [_completed(stdout="not json")])
    assert cmux_ops.new_ssh_workspace("host") is None


# --- send_text --------------------------------------------------------------

def test_send_text_targets_workspace_with_single_token(monkeypatch):
    calls = _stub_run(monkeypatch, [_completed()])
    cmux_ops.send_text("ws-9", "bash -lc 'vv --local'\\n")
    # The command is one token after `--` so its spaces reach the remote shell.
    assert calls[0] == ["send", "--workspace", "ws-9", "--", "bash -lc 'vv --local'\\n"]


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
