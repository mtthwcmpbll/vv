"""Tests for agent CLI discovery."""

from __future__ import annotations

import pytest

from vv import agents


def _which(installed: set[str]):
    """Return a fake ``shutil.which`` that only resolves ``installed`` commands."""
    return lambda cmd, *args, **kwargs: f"/usr/bin/{cmd}" if cmd in installed else None


def test_default_agent_is_a_known_agent():
    assert agents.DEFAULT_AGENT in agents.KNOWN_AGENTS


@pytest.mark.parametrize(
    "agent, expected",
    [
        ("claude", "claude"),
        ("claude --dangerously-skip-permissions", "claude"),
        ("  codex  run  ", "codex"),
    ],
)
def test_command_of_extracts_executable(agent, expected):
    assert agents._command_of(agent) == expected


def test_installed_agents_filters_and_preserves_known_order(monkeypatch):
    monkeypatch.setattr("shutil.which", _which({"gemini", "claude"}))
    # KNOWN_AGENTS lists claude before gemini, and that order must hold.
    assert agents.installed_agents() == ["claude", "gemini"]


def test_installed_agents_empty_when_nothing_on_path(monkeypatch):
    monkeypatch.setattr("shutil.which", _which(set()))
    assert agents.installed_agents() == []


def test_is_installed_checks_the_first_token(monkeypatch):
    monkeypatch.setattr("shutil.which", _which({"claude"}))
    assert agents.is_installed("claude") is True
    assert agents.is_installed("claude --resume") is True
    assert agents.is_installed("codex") is False
