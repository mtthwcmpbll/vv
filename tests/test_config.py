"""Tests for directory resolution and config-file parsing."""

from __future__ import annotations

import pytest

from vv import config


# --- directory resolution ---------------------------------------------------

def test_workspaces_dir_honors_env_and_creates_it(monkeypatch, tmp_path):
    target = tmp_path / "ws"
    monkeypatch.setenv("WORKSPACES_DIR", str(target))
    result = config.workspaces_dir()
    assert result == target
    assert result.is_dir()


def test_workspaces_dir_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.delenv("WORKSPACES_DIR", raising=False)
    default = tmp_path / "default-ws"
    monkeypatch.setattr(config, "DEFAULT_WORKSPACES_DIR", default)
    result = config.workspaces_dir()
    assert result == default
    assert result.is_dir()


def test_worktrees_dir_honors_env_and_creates_it(monkeypatch, tmp_path):
    target = tmp_path / "wt"
    monkeypatch.setenv("WORKTREES_DIR", str(target))
    result = config.worktrees_dir()
    assert result == target
    assert result.is_dir()


def test_chats_dir_lives_under_worktrees_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKTREES_DIR", str(tmp_path / "wt"))
    result = config.chats_dir()
    assert result == tmp_path / "wt" / "_chats"
    assert result.is_dir()


# --- config file location ---------------------------------------------------

def test_config_file_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("VV_CONFIG", str(tmp_path / "custom.toml"))
    assert config.config_file() == tmp_path / "custom.toml"


def test_config_file_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.delenv("VV_CONFIG", raising=False)
    default = tmp_path / "config.toml"
    monkeypatch.setattr(config, "DEFAULT_CONFIG_FILE", default)
    assert config.config_file() == default


# --- configured_agent -------------------------------------------------------

def _use_config(monkeypatch, tmp_path, body: str):
    """Write ``body`` to a config file and point VV_CONFIG at it."""
    path = tmp_path / "config.toml"
    path.write_text(body)
    monkeypatch.setenv("VV_CONFIG", str(path))
    return path


def test_configured_agent_is_none_without_a_file(monkeypatch, tmp_path):
    monkeypatch.setenv("VV_CONFIG", str(tmp_path / "missing.toml"))
    assert config.configured_agent() is None


def test_configured_agent_reads_the_value(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, 'agent = "codex"\n')
    assert config.configured_agent() == "codex"


def test_configured_agent_strips_surrounding_whitespace(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, 'agent = "  codex  "\n')
    assert config.configured_agent() == "codex"


def test_configured_agent_is_none_when_key_missing(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, 'other = "value"\n')
    assert config.configured_agent() is None


def test_configured_agent_is_none_when_blank(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, 'agent = "   "\n')
    assert config.configured_agent() is None


def test_configured_agent_is_none_when_not_a_string(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, "agent = 123\n")
    assert config.configured_agent() is None


def test_malformed_config_raises_config_error(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, 'agent = "unterminated\n')
    with pytest.raises(config.ConfigError):
        config.configured_agent()
