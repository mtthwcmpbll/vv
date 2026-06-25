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
def captured(monkeypatch, tmp_path):
    """Run `vv <url>` with `_start_from_url` stubbed; yield what it captured.

    `run(*args)` returns a dict with the resolved `agent` and `bypass`. Starts
    from a clean slate: no `$VV_AGENT`, and `$VV_CONFIG` pointed at a
    non-existent file so individual tests opt into env/config explicitly.
    """
    seen: dict = {}
    monkeypatch.setattr(
        cli,
        "_start_from_url",
        lambda url, agent, bypass, name=None: seen.update(
            agent=agent, bypass=bypass, name=name
        ),
    )
    monkeypatch.delenv("VV_AGENT", raising=False)
    monkeypatch.setenv("VV_CONFIG", str(tmp_path / "missing.toml"))

    def run(*args: str) -> dict:
        result = runner.invoke(cli.app, [*args, _REPO_URL])
        assert result.exit_code == 0, result.output
        return seen

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


def test_defaults_to_claude(captured):
    assert captured()["agent"] == "claude"


def test_vv_agent_env_is_used(captured, monkeypatch):
    monkeypatch.setenv("VV_AGENT", "codex")
    assert captured()["agent"] == "codex"


def test_config_file_is_used_when_no_flag_or_env(captured, monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, "gemini")
    assert captured()["agent"] == "gemini"


def test_vv_agent_env_beats_config_file(captured, monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, "gemini")
    monkeypatch.setenv("VV_AGENT", "codex")
    assert captured()["agent"] == "codex"


def test_agent_flag_beats_env_and_config(captured, monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, "gemini")
    monkeypatch.setenv("VV_AGENT", "codex")
    assert captured("--agent", "claude")["agent"] == "claude"


# --- bypass / --ask resolution ----------------------------------------------

def test_bypass_is_on_by_default(captured):
    assert captured()["bypass"] is True


def test_ask_flag_disables_bypass(captured):
    assert captured("--ask")["bypass"] is False


def test_no_ask_flag_keeps_bypass(captured):
    assert captured("--no-ask")["bypass"] is True


def test_config_ask_true_disables_bypass(captured, monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("ask = true\n")
    monkeypatch.setenv("VV_CONFIG", str(cfg))
    assert captured()["bypass"] is False


def test_ask_flag_overrides_config(captured, monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("ask = false\n")  # config would bypass
    monkeypatch.setenv("VV_CONFIG", str(cfg))
    assert captured("--ask")["bypass"] is False  # flag wins -> ask


def test_no_ask_flag_overrides_config(captured, monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("ask = true\n")  # config would ask
    monkeypatch.setenv("VV_CONFIG", str(cfg))
    assert captured("--no-ask")["bypass"] is True  # flag wins -> bypass


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


# --- _resume_worktree bypass mode -------------------------------------------

@pytest.fixture
def sent_command(monkeypatch):
    """Stub tmux around a fresh-session launch; yield the command it sends."""
    sent: list[str] = []
    monkeypatch.setattr(cli.tmux_ops, "session_exists", lambda name: False)
    monkeypatch.setattr(cli.tmux_ops, "create_session", lambda name, cwd: None)
    monkeypatch.setattr(cli.tmux_ops, "send_command", lambda name, cmd: sent.append(cmd))
    monkeypatch.setattr(cli.tmux_ops, "attach", lambda name: None)
    return sent


def test_resume_worktree_appends_bypass_flag(sent_command, tmp_path):
    cli._resume_worktree("falcon", tmp_path, "claude", bypass=True)
    assert sent_command == ["claude " + cli.agents.BYPASS_FLAGS["claude"]]


def test_resume_worktree_without_bypass_sends_bare_agent(sent_command, tmp_path):
    cli._resume_worktree("falcon", tmp_path, "claude", bypass=False)
    assert sent_command == ["claude"]


def test_resume_worktree_bypass_leaves_unknown_agent_unflagged(sent_command, tmp_path):
    cli._resume_worktree("falcon", tmp_path, "agy", bypass=True)
    assert sent_command == ["agy"]


# --- chat-only sessions ------------------------------------------------------

@pytest.fixture
def chat_env(monkeypatch, tmp_path):
    """Point WORKSPACES_DIR / WORKTREES_DIR at tmp_path and stub tmux entirely."""
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("WORKTREES_DIR", str(tmp_path / "wt"))
    monkeypatch.delenv("VV_AGENT", raising=False)
    monkeypatch.setenv("VV_CONFIG", str(tmp_path / "missing.toml"))

    # Stub tmux and agent-presence so nothing real runs.
    monkeypatch.setattr(cli.tmux_ops, "list_sessions", lambda *_a, **_k: [])
    monkeypatch.setattr(cli.tmux_ops, "session_exists", lambda name: False)
    monkeypatch.setattr(cli.tmux_ops, "create_session", lambda *a, **k: None)
    monkeypatch.setattr(cli.tmux_ops, "send_command", lambda *a, **k: None)
    monkeypatch.setattr(cli.tmux_ops, "attach", lambda *a, **k: None)
    monkeypatch.setattr(cli.tmux_ops, "kill_session", lambda *a, **k: None)
    monkeypatch.setattr(cli.agents, "is_installed", lambda _a: True)
    return tmp_path


def test_new_chat_session_creates_dir_under_chats(chat_env):
    cli._new_chat_session("claude", bypass=False)
    chats_root = chat_env / "wt" / "_chats"
    created = [p for p in chats_root.iterdir() if p.is_dir()]
    assert len(created) == 1
    # The picked name must be a real word from the curated pool.
    from vv.names import WORDS
    assert created[0].name in WORDS


def test_list_worktrees_surfaces_chats_under_sentinel(chat_env):
    cli._new_chat_session("claude", bypass=False)
    rows = cli._list_worktrees()
    assert len(rows) == 1
    repo, name, path = rows[0]
    assert repo == cli.CHATS
    assert path == chat_env / "wt" / "_chats" / name


def test_chat_name_avoids_existing_session_names(chat_env, monkeypatch):
    # Pre-create a chat dir named 'falcon', and force random_name to want it.
    (chat_env / "wt" / "_chats" / "falcon").mkdir(parents=True)
    calls = {"taken": None}

    def fake_random(taken):
        calls["taken"] = set(taken)
        return "otter"

    monkeypatch.setattr(cli.names, "random_name", fake_random)
    cli._new_chat_session("claude", bypass=False)
    assert "falcon" in calls["taken"]


def test_delete_chat_empty_skips_warning_and_removes_dir(chat_env, monkeypatch):
    chat_path = chat_env / "wt" / "_chats" / "falcon"
    chat_path.mkdir(parents=True)

    confirms: list = []
    monkeypatch.setattr(
        cli.questionary, "confirm",
        lambda *a, **k: confirms.append(a) or _Answer(False),
    )

    cli._delete_session(cli.CHATS, "falcon", chat_path, live=set())
    assert confirms == []          # empty dir -> no warning
    assert not chat_path.exists()  # actually removed


def test_delete_chat_nonempty_prompts_and_can_be_cancelled(chat_env, monkeypatch):
    chat_path = chat_env / "wt" / "_chats" / "falcon"
    chat_path.mkdir(parents=True)
    (chat_path / "notes.md").write_text("important\n")

    confirms: list = []
    monkeypatch.setattr(
        cli.questionary, "confirm",
        lambda *a, **k: confirms.append(a) or _Answer(False),
    )

    cli._delete_session(cli.CHATS, "falcon", chat_path, live=set())
    assert len(confirms) == 1      # warned about contents
    assert chat_path.exists()      # cancelled -> kept


def test_delete_chat_does_not_invoke_git_ops(chat_env, monkeypatch):
    chat_path = chat_env / "wt" / "_chats" / "falcon"
    chat_path.mkdir(parents=True)

    def boom(*a, **k):  # noqa: ARG001
        raise AssertionError("git_ops must not be called for chat sessions")

    monkeypatch.setattr(cli.git_ops, "is_dirty", boom)
    monkeypatch.setattr(cli.git_ops, "unpushed_count", boom)
    monkeypatch.setattr(cli.git_ops, "remove_worktree", boom)
    monkeypatch.setattr(cli.git_ops, "delete_branch", boom)

    cli._delete_session(cli.CHATS, "falcon", chat_path, live=set())
    assert not chat_path.exists()


def test_delete_chat_kills_live_tmux_session(chat_env, monkeypatch):
    chat_path = chat_env / "wt" / "_chats" / "falcon"
    chat_path.mkdir(parents=True)
    killed: list = []
    monkeypatch.setattr(cli.tmux_ops, "kill_session", killed.append)

    cli._delete_session(cli.CHATS, "falcon", chat_path, live={"falcon"})
    assert killed == ["falcon"]


def test_chat_flag_starts_new_chat_session(monkeypatch, tmp_path):
    """`vv --chat` routes to _new_chat_session with the resolved agent."""
    seen: dict = {}
    monkeypatch.setattr(
        cli, "_new_chat_session",
        lambda agent, bypass, name=None: seen.update(
            agent=agent, bypass=bypass, name=name
        ),
    )
    monkeypatch.delenv("VV_AGENT", raising=False)
    monkeypatch.setenv("VV_CONFIG", str(tmp_path / "missing.toml"))

    result = runner.invoke(cli.app, ["--chat", "--agent", "codex"])
    assert result.exit_code == 0, result.output
    assert seen == {"agent": "codex", "bypass": True, "name": None}


def test_chat_flag_rejects_repo_url(monkeypatch, tmp_path):
    """`vv --chat <url>` must error out — the combination is contradictory."""
    monkeypatch.delenv("VV_AGENT", raising=False)
    monkeypatch.setenv("VV_CONFIG", str(tmp_path / "missing.toml"))
    # Sanity: neither downstream entry point should be reached.
    monkeypatch.setattr(
        cli, "_new_chat_session",
        lambda agent, bypass, name=None: pytest.fail("called"),
    )
    monkeypatch.setattr(
        cli, "_start_from_url",
        lambda url, agent, bypass, name=None: pytest.fail("called"),
    )

    result = runner.invoke(cli.app, ["--chat", _REPO_URL])
    assert result.exit_code == 1
    assert "cannot be combined" in result.output


# --- explicit --name (used by remote forwarding) ----------------------------

def test_explicit_name_is_used_when_free(chat_env):
    cli._new_chat_session("claude", bypass=False, name="otter")
    assert (chat_env / "wt" / "_chats" / "otter").is_dir()


def test_explicit_name_collision_is_rejected(chat_env):
    import typer

    (chat_env / "wt" / "_chats" / "falcon").mkdir(parents=True)
    with pytest.raises(typer.Exit):
        cli._new_chat_session("claude", bypass=False, name="falcon")


# --- _menu_add_repo GitHub repo picker --------------------------------------

@pytest.fixture
def add_repo_harness(monkeypatch):
    """Stub _start_from_url / _pick_agent / the picker around _menu_add_repo.

    ``configure(repos=..., picked=..., typed=..., available=...)`` wires gh_ops,
    the repo picker (returns ``picked``) and the URL text prompt (returns
    ``typed``); returns a dict capturing the URL handed to _start_from_url.
    """
    seen: dict = {}
    monkeypatch.setattr(
        cli, "_start_from_url",
        lambda url, agent, bypass, name=None: seen.update(url=url, agent=agent),
    )
    monkeypatch.setattr(cli, "_pick_agent", lambda default: "claude")

    def configure(*, repos, picked=None, typed=None, available=True, protocol="ssh"):
        monkeypatch.setattr(cli.gh_ops, "is_available", lambda: available)
        monkeypatch.setattr(cli.gh_ops, "list_repos", lambda: repos)
        monkeypatch.setattr(cli.config, "configured_clone_protocol", lambda: protocol)
        monkeypatch.setattr(
            cli.gh_ops, "clone_url",
            lambda nwo, proto="ssh": f"{proto}://github.com/{nwo}.git",
        )
        monkeypatch.setattr(cli, "_pick_repo", lambda _repos: picked)
        monkeypatch.setattr(cli.questionary, "text", lambda *a, **k: _Answer(typed))
        return seen

    return configure


def test_add_repo_picked_repo_uses_configured_protocol(add_repo_harness):
    seen = add_repo_harness(repos=["octo/repo"], picked="octo/repo", protocol="ssh")
    cli._menu_add_repo("claude", bypass=True)
    assert seen["url"] == "ssh://github.com/octo/repo.git"  # default protocol


def test_add_repo_picked_repo_honors_https_protocol(add_repo_harness):
    seen = add_repo_harness(repos=["octo/repo"], picked="octo/repo", protocol="https")
    cli._menu_add_repo("claude", bypass=True)
    assert seen["url"] == "https://github.com/octo/repo.git"


def test_add_repo_enter_url_sentinel_prompts_for_url(add_repo_harness):
    url = "git@example.com:team/thing.git"
    seen = add_repo_harness(repos=["octo/repo"], picked=cli._ENTER_URL, typed=url)
    cli._menu_add_repo("claude", bypass=True)
    assert seen["url"] == url  # sentinel -> free-text URL, passed through verbatim


def test_add_repo_falls_back_to_text_when_gh_unavailable(add_repo_harness):
    url = "https://example.com/owner/repo.git"
    seen = add_repo_harness(repos=[], typed=url, available=False)
    cli._menu_add_repo("claude", bypass=True)
    assert seen["url"] == url


def test_add_repo_cancelled_picker_aborts(add_repo_harness):
    seen = add_repo_harness(repos=["octo/repo"], picked=None)
    cli._menu_add_repo("claude", bypass=True)
    assert seen == {}  # cancelled -> no session started


def test_add_repo_blank_url_aborts(add_repo_harness):
    seen = add_repo_harness(repos=["octo/repo"], picked=cli._ENTER_URL, typed="")
    cli._menu_add_repo("claude", bypass=True)
    assert seen == {}  # empty URL -> no session started


def test_cap_select_rows_limits_choice_window_height():
    """_cap_select_rows caps the choices window so long lists scroll, not flood."""
    question = cli.questionary.select(
        "m",
        choices=[f"r{i}" for i in range(20)],
        use_search_filter=True,
        use_jk_keys=False,
    )
    cli._cap_select_rows(question, 5)
    capped = [
        c.height.max
        for c in question.application.layout.walk()
        if type(getattr(c, "content", None)).__name__ == "InquirerControl"
    ]
    assert capped == [5]


# --- remote-launcher mode ----------------------------------------------------

@pytest.fixture
def remote_harness(monkeypatch, tmp_path):
    """Stub remote.launch / gen_name and make local git/tmux explode.

    Yields ``(seen, write_config)``: ``seen`` records the `remote.launch` call;
    ``write_config(body)`` drops a config file and points VV_CONFIG at it.
    """
    seen: dict = {}
    monkeypatch.setattr(
        cli.remote, "launch",
        lambda remote_cfg, *, remote_argv, title: seen.update(
            remote=remote_cfg, argv=remote_argv, title=title
        ),
    )
    monkeypatch.setattr(cli.remote, "gen_name", lambda: "otter")
    monkeypatch.delenv("VV_AGENT", raising=False)
    monkeypatch.delenv("VV_REMOTE", raising=False)

    def boom(*a, **k):  # noqa: ARG001
        raise AssertionError("local git/tmux must not run in remote mode")

    for fn in ("clone", "fetch", "add_worktree", "existing_branches"):
        monkeypatch.setattr(cli.git_ops, fn, boom)
    monkeypatch.setattr(cli.tmux_ops, "create_session", boom)

    def write_config(body: str) -> None:
        path = tmp_path / "config.toml"
        path.write_text(body)
        monkeypatch.setenv("VV_CONFIG", str(path))

    return seen, write_config


def test_remote_flag_routes_bare_vv_to_remote_tui(remote_harness):
    seen, write_config = remote_harness
    write_config('[remote]\nhost = "myserver"\n')
    result = runner.invoke(cli.app, ["--remote"])
    assert result.exit_code == 0, result.output
    assert seen["argv"] == ["--local"]      # nothing extra -> remote opens its TUI
    assert seen["title"] == "myserver"      # generic host title
    assert seen["remote"].host == "myserver"


def test_config_mode_remote_routes_to_remote_launch(remote_harness):
    seen, write_config = remote_harness
    write_config('mode = "remote"\n[remote]\nhost = "h"\n')
    result = runner.invoke(cli.app, [])
    assert result.exit_code == 0, result.output
    assert seen["argv"] == ["--local"]


def test_local_flag_overrides_config_remote(remote_harness, monkeypatch):
    seen, write_config = remote_harness
    write_config('mode = "remote"\n[remote]\nhost = "h"\n')
    ran: dict = {}
    monkeypatch.setattr(
        cli, "_interactive_menu", lambda agent, bypass: ran.setdefault("local", True)
    )
    result = runner.invoke(cli.app, ["--local"])
    assert result.exit_code == 0, result.output
    assert ran == {"local": True}
    assert seen == {}                       # remote.launch never called


def test_remote_url_forwards_mirrored_name_and_url(remote_harness):
    seen, write_config = remote_harness
    write_config('[remote]\nhost = "h"\n')
    result = runner.invoke(cli.app, ["--remote", _REPO_URL])
    assert result.exit_code == 0, result.output
    assert seen["argv"] == ["--name", "otter", "--local", _REPO_URL]
    assert seen["title"] == "otter"


def test_remote_chat_forwards_chat_flag(remote_harness):
    seen, write_config = remote_harness
    write_config('[remote]\nhost = "h"\n')
    result = runner.invoke(cli.app, ["--remote", "--chat"])
    assert result.exit_code == 0, result.output
    assert seen["argv"] == ["--name", "otter", "--local", "--chat"]


def test_remote_forwards_agent_and_ask(remote_harness):
    seen, write_config = remote_harness
    write_config('[remote]\nhost = "h"\n')
    result = runner.invoke(cli.app, ["--remote", "--agent", "codex", "--ask"])
    assert result.exit_code == 0, result.output
    assert seen["argv"] == ["--local", "--agent", "codex", "--ask"]


def test_explicit_name_is_forwarded_verbatim(remote_harness):
    seen, write_config = remote_harness
    write_config('[remote]\nhost = "h"\n')
    result = runner.invoke(cli.app, ["--remote", "--name", "raven", _REPO_URL])
    assert result.exit_code == 0, result.output
    assert seen["argv"] == ["--name", "raven", "--local", _REPO_URL]


def test_remote_mode_without_remote_config_errors(remote_harness):
    seen, write_config = remote_harness
    write_config('mode = "remote"\n')      # no [remote] table
    result = runner.invoke(cli.app, [])
    assert result.exit_code == 1
    assert "no [remote] is configured" in result.output
    assert seen == {}
