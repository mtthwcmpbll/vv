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


# --- read_screen ------------------------------------------------------------

def test_read_screen_returns_text_field(monkeypatch):
    calls = _stub_run(monkeypatch, [_completed(stdout='{"text": "matt@box:~$ "}')])
    assert cmux_ops.read_screen("ws-1") == "matt@box:~$ "
    assert calls[0] == ["read-screen", "--workspace", "ws-1", "--json"]


def test_read_screen_is_empty_when_unreadable(monkeypatch):
    _stub_run(monkeypatch, [_completed(returncode=1)])
    assert cmux_ops.read_screen("ws-1") == ""


def test_read_screen_is_empty_on_bad_json(monkeypatch):
    _stub_run(monkeypatch, [_completed(stdout="not json")])
    assert cmux_ops.read_screen("ws-1") == ""


# --- wait_until_ready -------------------------------------------------------

def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(cmux_ops.time, "sleep", lambda _s: None)


def test_wait_until_ready_returns_true_on_prompt(monkeypatch):
    _no_real_sleep(monkeypatch)
    screens = iter(["", "Last login: ...\nmatt@box:~$ "])
    monkeypatch.setattr(cmux_ops, "read_screen", lambda _w: next(screens))
    assert cmux_ops.wait_until_ready("ws-1") is True


def test_wait_until_ready_accepts_a_quiet_unrecognised_prompt(monkeypatch):
    _no_real_sleep(monkeypatch)
    # No prompt sigil, but the screen has gone quiet (same two polls running).
    screens = iter(["welcome banner", "welcome banner"])
    monkeypatch.setattr(cmux_ops, "read_screen", lambda _w: next(screens))
    assert cmux_ops.wait_until_ready("ws-1") is True


def test_wait_until_ready_sleeps_the_head_start_delay_first(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(cmux_ops.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(cmux_ops, "read_screen", lambda _w: "matt@box:~$ ")
    assert cmux_ops.wait_until_ready("ws-1", delay=2.0) is True
    # The 2s head start is slept before any polling; it isn't a poll interval.
    assert slept and slept[0] == 2.0


def test_wait_until_ready_skips_delay_when_zero(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(cmux_ops.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(cmux_ops, "read_screen", lambda _w: "matt@box:~$ ")
    assert cmux_ops.wait_until_ready("ws-1", delay=0.0) is True
    assert slept == []  # prompt found on first poll, no sleeps at all


def test_wait_until_ready_times_out_when_never_ready(monkeypatch):
    _no_real_sleep(monkeypatch)
    # An always-blank screen is neither a prompt nor "stable non-empty".
    monkeypatch.setattr(cmux_ops, "read_screen", lambda _w: "")
    clock = iter([0.0, 0.5, 1.0, 1.5])  # crosses the 1.0s timeout
    monkeypatch.setattr(cmux_ops.time, "monotonic", lambda: next(clock))
    assert cmux_ops.wait_until_ready("ws-1", timeout=1.0) is False


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
