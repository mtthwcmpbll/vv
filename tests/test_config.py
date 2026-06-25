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


# --- configured_ask ---------------------------------------------------------

def test_configured_ask_is_false_without_a_file(monkeypatch, tmp_path):
    monkeypatch.setenv("VV_CONFIG", str(tmp_path / "missing.toml"))
    assert config.configured_ask() is False


def test_configured_ask_reads_true(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, "ask = true\n")
    assert config.configured_ask() is True


def test_configured_ask_reads_false(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, "ask = false\n")
    assert config.configured_ask() is False


def test_configured_ask_ignores_non_boolean_values(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, 'ask = "yes"\n')
    assert config.configured_ask() is False


# --- configured_mode --------------------------------------------------------

def test_configured_mode_defaults_to_local_without_a_file(monkeypatch, tmp_path):
    monkeypatch.setenv("VV_CONFIG", str(tmp_path / "missing.toml"))
    assert config.configured_mode() == "local"


def test_configured_mode_reads_remote(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, 'mode = "remote"\n')
    assert config.configured_mode() == "remote"


def test_configured_mode_unknown_value_falls_back_to_local(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, 'mode = "potato"\n')
    assert config.configured_mode() == "local"


# --- configured_remote ------------------------------------------------------

def test_configured_remote_is_none_without_a_table(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, 'mode = "remote"\n')
    assert config.configured_remote() is None


def test_configured_remote_parses_full_table(monkeypatch, tmp_path):
    _use_config(
        monkeypatch,
        tmp_path,
        "[remote]\n"
        'host = "myserver"\n'
        'user = "matt"\n'
        "port = 2222\n"
        'identity = "~/.ssh/id_ed25519"\n'
        'ssh_options = ["StrictHostKeyChecking=no"]\n'
        'vv_command = "~/.local/bin/vv"\n'
        "ready_delay = 2\n"
        "ready_timeout = 30\n"
        "ready_interval = 0.5\n",
    )
    remote = config.configured_remote()
    assert remote == config.Remote(
        host="myserver",
        user="matt",
        port=2222,
        identity="~/.ssh/id_ed25519",
        ssh_options=("StrictHostKeyChecking=no",),
        vv_command="~/.local/bin/vv",
        ready_delay=2.0,
        ready_timeout=30.0,
        ready_interval=0.5,
    )


def test_configured_remote_defaults_optional_fields(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, '[remote]\nhost = "myserver"\n')
    remote = config.configured_remote()
    assert remote == config.Remote(host="myserver")
    assert remote.vv_command == "vv"
    assert remote.ready_delay == 0.0
    assert remote.ready_timeout == 20.0
    assert remote.ready_interval == 0.4


def test_configured_remote_allows_zero_ready_delay(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, '[remote]\nhost = "h"\nready_delay = 0\n')
    assert config.configured_remote().ready_delay == 0.0


def test_configured_remote_rejects_negative_ready_delay(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, '[remote]\nhost = "h"\nready_delay = -1\n')
    with pytest.raises(config.ConfigError, match="ready_delay"):
        config.configured_remote()


def test_configured_remote_rejects_non_positive_ready_timeout(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, '[remote]\nhost = "h"\nready_timeout = 0\n')
    with pytest.raises(config.ConfigError, match="ready_timeout"):
        config.configured_remote()


def test_configured_remote_rejects_non_numeric_ready_interval(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, '[remote]\nhost = "h"\nready_interval = "fast"\n')
    with pytest.raises(config.ConfigError, match="ready_interval"):
        config.configured_remote()


def test_configured_remote_requires_host(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, '[remote]\nuser = "matt"\n')
    with pytest.raises(config.ConfigError):
        config.configured_remote()


def test_configured_remote_rejects_non_integer_port(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, '[remote]\nhost = "h"\nport = "22"\n')
    with pytest.raises(config.ConfigError):
        config.configured_remote()


def test_configured_remote_rejects_bad_ssh_options(monkeypatch, tmp_path):
    _use_config(monkeypatch, tmp_path, '[remote]\nhost = "h"\nssh_options = "nope"\n')
    with pytest.raises(config.ConfigError):
        config.configured_remote()
