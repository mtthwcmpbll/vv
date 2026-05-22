"""Tests for agent resolution in the CLI entry point.

Only the precedence chain is exercised here: `_start_from_url` is stubbed so
no real clone/worktree/tmux work happens.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from vv import cli

runner = CliRunner()
_REPO_URL = "https://example.com/owner/repo.git"


@pytest.fixture
def captured_agent(monkeypatch, tmp_path):
    """Run `vv <url>` with `_start_from_url` stubbed; yield the resolved agent.

    Starts from a clean slate: no `$VV_AGENT`, and `$VV_CONFIG` pointed at a
    non-existent file so individual tests opt into env/config explicitly.
    """
    seen: dict[str, str] = {}
    monkeypatch.setattr(cli, "_start_from_url", lambda url, agent: seen.update(agent=agent))
    monkeypatch.delenv("VV_AGENT", raising=False)
    monkeypatch.setenv("VV_CONFIG", str(tmp_path / "missing.toml"))

    def run(*args: str) -> str:
        result = runner.invoke(cli.app, [*args, _REPO_URL])
        assert result.exit_code == 0, result.output
        return seen["agent"]

    return run


def _write_config(monkeypatch, tmp_path, agent: str) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'agent = "{agent}"\n')
    monkeypatch.setenv("VV_CONFIG", str(cfg))


def test_banner_renders_the_wordmark(capsys):
    cli._banner()
    out = capsys.readouterr().out
    assert "vv" in out
    assert "worktree sessions" in out
    assert "◍" in out  # the branch-diagram glyph


def test_defaults_to_claude(captured_agent):
    assert captured_agent() == "claude"


def test_vv_agent_env_is_used(captured_agent, monkeypatch):
    monkeypatch.setenv("VV_AGENT", "codex")
    assert captured_agent() == "codex"


def test_config_file_is_used_when_no_flag_or_env(captured_agent, monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, "gemini")
    assert captured_agent() == "gemini"


def test_vv_agent_env_beats_config_file(captured_agent, monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, "gemini")
    monkeypatch.setenv("VV_AGENT", "codex")
    assert captured_agent() == "codex"


def test_agent_flag_beats_env_and_config(captured_agent, monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, "gemini")
    monkeypatch.setenv("VV_AGENT", "codex")
    assert captured_agent("--agent", "claude") == "claude"


# --- _delete_session safety prompt ------------------------------------------

class _Answer:
    """Stand-in for a questionary prompt with a canned answer."""

    def __init__(self, answer):
        self._answer = answer

    def ask(self):
        return self._answer


@pytest.fixture
def delete_harness(monkeypatch, tmp_path):
    """Stub git/tmux around `_delete_session` and record what it did."""
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path / "ws"))
    calls: dict[str, list] = {"removed": [], "branches": [], "killed": [], "confirms": []}

    monkeypatch.setattr(cli.git_ops, "remove_worktree",
                        lambda ws, p, force=False: calls["removed"].append(force))
    monkeypatch.setattr(cli.git_ops, "delete_branch",
                        lambda ws, b, force=False: calls["branches"].append((b, force)))
    monkeypatch.setattr(cli.tmux_ops, "kill_session",
                        lambda name: calls["killed"].append(name))

    def configure(*, dirty=False, unpushed=0, confirm=True):
        monkeypatch.setattr(cli.git_ops, "is_dirty", lambda p: dirty)
        monkeypatch.setattr(cli.git_ops, "unpushed_count", lambda p: unpushed)

        def fake_confirm(*args, **kwargs):
            calls["confirms"].append(args[0] if args else "")
            return _Answer(confirm)

        monkeypatch.setattr(cli.questionary, "confirm", fake_confirm)
        return calls

    return configure


def test_delete_clean_worktree_skips_the_warning(delete_harness, tmp_path):
    calls = delete_harness(dirty=False, unpushed=0)
    cli._delete_session("repo", "falcon", tmp_path / "wt", live=set())
    assert calls["confirms"] == []          # nothing at risk -> no prompt
    assert calls["removed"] == [True]       # force-removed
    assert calls["branches"] == [("falcon", True)]


def test_delete_dirty_worktree_prompts_and_can_be_cancelled(delete_harness, tmp_path):
    calls = delete_harness(dirty=True, confirm=False)
    cli._delete_session("repo", "falcon", tmp_path / "wt", live=set())
    assert len(calls["confirms"]) == 1      # warned
    assert calls["removed"] == []           # declined -> nothing deleted
    assert calls["branches"] == []


def test_delete_dirty_worktree_proceeds_when_confirmed(delete_harness, tmp_path):
    calls = delete_harness(dirty=True, confirm=True)
    cli._delete_session("repo", "falcon", tmp_path / "wt", live=set())
    assert len(calls["confirms"]) == 1
    assert calls["removed"] == [True]
    assert calls["branches"] == [("falcon", True)]


def test_delete_warns_on_unpushed_commits(delete_harness, tmp_path):
    calls = delete_harness(dirty=False, unpushed=2, confirm=False)
    cli._delete_session("repo", "falcon", tmp_path / "wt", live=set())
    assert len(calls["confirms"]) == 1      # unpushed commits trigger the prompt
    assert calls["removed"] == []


def test_delete_kills_a_live_session_first(delete_harness, tmp_path):
    calls = delete_harness(dirty=False, unpushed=0)
    cli._delete_session("repo", "falcon", tmp_path / "wt", live={"falcon"})
    assert calls["killed"] == ["falcon"]


def test_delete_does_not_kill_when_no_live_session(delete_harness, tmp_path):
    calls = delete_harness(dirty=False, unpushed=0)
    cli._delete_session("repo", "falcon", tmp_path / "wt", live=set())
    assert calls["killed"] == []
