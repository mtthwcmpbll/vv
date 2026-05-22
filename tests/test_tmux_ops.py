"""Tests for the tmux CLI wrappers.

These never touch a real tmux server: ``_run`` is stubbed so the tests
exercise output parsing and argument construction in isolation.
"""

from __future__ import annotations

import subprocess

import pytest

from vv import tmux_ops


def _completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["tmux"], returncode=returncode, stdout=stdout, stderr=""
    )


def _stub_run(monkeypatch, result):
    """Make ``tmux_ops._run`` return ``result`` and record the args it sees."""
    calls: list[list[str]] = []

    def fake(args, **kwargs):
        calls.append(args)
        return result

    monkeypatch.setattr(tmux_ops, "_run", fake)
    return calls


# --- list_sessions ----------------------------------------------------------

def test_list_sessions_empty_when_no_server(monkeypatch):
    _stub_run(monkeypatch, _completed(returncode=1))
    assert tmux_ops.list_sessions() == []


def test_list_sessions_parses_names_and_skips_blank_lines(monkeypatch):
    _stub_run(monkeypatch, _completed(stdout="alpha\t1\nbeta\t\n\ngamma\t1\n"))
    assert tmux_ops.list_sessions() == ["alpha", "beta", "gamma"]


def test_list_sessions_vv_only_keeps_tagged_sessions(monkeypatch):
    _stub_run(monkeypatch, _completed(stdout="alpha\t1\nbeta\t\ngamma\t1\n"))
    assert tmux_ops.list_sessions(vv_only=True) == ["alpha", "gamma"]


# --- session_exists ---------------------------------------------------------

def test_session_exists_reflects_return_code(monkeypatch):
    _stub_run(monkeypatch, _completed(returncode=0))
    assert tmux_ops.session_exists("falcon") is True
    _stub_run(monkeypatch, _completed(returncode=1))
    assert tmux_ops.session_exists("falcon") is False


# --- argument construction --------------------------------------------------

def test_create_session_roots_at_cwd_and_stamps_vv_tag(monkeypatch, tmp_path):
    calls = _stub_run(monkeypatch, _completed())
    tmux_ops.create_session("falcon", tmp_path)
    assert calls[0] == ["new-session", "-d", "-s", "falcon", "-c", str(tmp_path)]
    assert calls[1] == ["set-option", "-t", "=falcon:", tmux_ops.VV_TAG, "1"]


def test_send_command_targets_session_with_trailing_colon(monkeypatch):
    calls = _stub_run(monkeypatch, _completed())
    tmux_ops.send_command("falcon", "claude")
    assert calls == [["send-keys", "-t", "=falcon:", "claude", "Enter"]]


def test_kill_session_targets_the_session(monkeypatch):
    calls = _stub_run(monkeypatch, _completed())
    tmux_ops.kill_session("falcon")
    assert calls == [["kill-session", "-t", "=falcon"]]


def test_attach_switches_client_when_inside_tmux(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    calls = _stub_run(monkeypatch, _completed())
    tmux_ops.attach("falcon")
    assert calls == [["switch-client", "-t", "=falcon"]]


def test_attach_execs_tmux_when_outside_tmux(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    execs: list[tuple] = []
    monkeypatch.setattr(tmux_ops.os, "execvp", lambda file, args: execs.append((file, args)))
    tmux_ops.attach("falcon")
    assert execs == [("tmux", ["tmux", "attach-session", "-t", "=falcon"])]


# --- _run error handling ----------------------------------------------------

def test_run_raises_tmux_error_when_tmux_is_missing(monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(tmux_ops.TmuxError, match="not installed"):
        tmux_ops._run(["list-sessions"])


def test_run_raises_tmux_error_on_command_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "tmux", stderr="boom")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(tmux_ops.TmuxError, match="boom"):
        tmux_ops._run(["kill-server"])
