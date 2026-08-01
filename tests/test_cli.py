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
        lambda url, agent, bypass, name=None, pending_notes=None: seen.update(
            agent=agent, bypass=bypass, name=name, notes=pending_notes
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


def test_emit_cwd_short_circuits_to_tmux_ops(monkeypatch):
    """`vv --emit-cwd PATH` emits and exits without touching the normal flows."""
    seen: dict = {}
    monkeypatch.setattr(cli.tmux_ops, "emit_cwd", lambda cwd: seen.update(cwd=cwd))
    monkeypatch.setattr(
        cli, "_interactive_menu", lambda *a, **k: pytest.fail("should not run")
    )
    result = runner.invoke(cli.app, ["--emit-cwd", "/work/tree"])
    assert result.exit_code == 0, result.output
    assert str(seen["cwd"]) == "/work/tree"


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


# --- _delete_repo (delete a whole cloned repo + its worktrees) ---------------

@pytest.fixture
def repo_delete_harness(monkeypatch, tmp_path):
    """Lay out a fake repo + worktrees on disk and stub git/tmux around it."""
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("WORKTREES_DIR", str(tmp_path / "wt"))

    workspace = tmp_path / "ws" / "repo"
    workspace.mkdir(parents=True)
    worktrees_root = tmp_path / "wt" / "repo"

    calls: dict[str, list] = {"killed": [], "confirms": []}
    monkeypatch.setattr(cli.tmux_ops, "kill_session",
                        lambda name: calls["killed"].append(name))
    monkeypatch.setattr(cli.tmux_ops, "list_sessions", lambda *a, **k: [])
    monkeypatch.setattr(cli.git_ops, "is_dirty", lambda p: False)
    monkeypatch.setattr(cli.git_ops, "unpushed_count", lambda p: 0)

    def configure(*, worktrees=(), live=(), confirm=True):
        paths = []
        for name in worktrees:
            p = worktrees_root / name
            p.mkdir(parents=True)
            paths.append((name, p))
        monkeypatch.setattr(
            cli, "_list_worktrees", lambda: [("repo", n, p) for n, p in paths]
        )
        monkeypatch.setattr(cli.tmux_ops, "list_sessions", lambda *a, **k: list(live))

        def fake_confirm(*args, **kwargs):
            calls["confirms"].append(args[0] if args else "")
            return _Answer(confirm)

        monkeypatch.setattr(cli.questionary, "confirm", fake_confirm)
        return calls, workspace, worktrees_root

    return configure


def test_delete_repo_removes_clone_and_worktrees(repo_delete_harness):
    calls, workspace, worktrees_root = repo_delete_harness(worktrees=["falcon"])
    cli._delete_repo("repo")
    assert len(calls["confirms"]) == 1     # always confirms
    assert not workspace.exists()          # clone gone
    assert not worktrees_root.exists()     # worktrees gone


def test_delete_repo_can_be_cancelled(repo_delete_harness):
    calls, workspace, worktrees_root = repo_delete_harness(
        worktrees=["falcon"], confirm=False
    )
    cli._delete_repo("repo")
    assert len(calls["confirms"]) == 1
    assert workspace.exists()              # declined -> nothing removed
    assert worktrees_root.exists()


def test_delete_repo_kills_live_worktree_sessions(repo_delete_harness):
    calls, _ws, _wt = repo_delete_harness(worktrees=["falcon"], live=["falcon"])
    cli._delete_repo("repo")
    assert calls["killed"] == ["falcon"]


def test_delete_repo_with_no_worktrees_still_confirms(repo_delete_harness):
    calls, workspace, _wt = repo_delete_harness(worktrees=[])
    cli._delete_repo("repo")
    assert len(calls["confirms"]) == 1
    assert not workspace.exists()


# --- _resume_worktree bypass mode -------------------------------------------

@pytest.fixture
def sent_command(monkeypatch):
    """Stub tmux around a fresh-session launch; yield the command it sends."""
    sent: list[str] = []
    monkeypatch.setattr(cli.tmux_ops, "session_exists", lambda name: False)
    monkeypatch.setattr(cli.tmux_ops, "create_session", lambda name, cwd: None)
    monkeypatch.setattr(
        cli.tmux_ops, "send_command", lambda name, cmd, **_k: sent.append(cmd)
    )
    monkeypatch.setattr(cli.tmux_ops, "attach", lambda *a, **k: None)
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


def test_resume_worktree_cd_guards_command_into_worktree(monkeypatch, tmp_path):
    # The launch command must be cd-guarded into the worktree so a poisoned tmux
    # server (own cwd deleted) can't strand the agent in a dead directory.
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli.tmux_ops, "session_exists", lambda name: False)
    monkeypatch.setattr(cli.tmux_ops, "create_session", lambda name, cwd: None)
    monkeypatch.setattr(
        cli.tmux_ops,
        "send_command",
        lambda name, cmd, **kw: captured.update(cmd=cmd, cwd=kw.get("cwd")),
    )
    monkeypatch.setattr(cli.tmux_ops, "attach", lambda *a, **k: None)
    cli._resume_worktree("falcon", tmp_path, "claude", bypass=False)
    assert captured["cwd"] == tmp_path


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
        lambda agent, bypass, name=None, pending_notes=None: seen.update(
            agent=agent, bypass=bypass, name=name, notes=pending_notes
        ),
    )
    monkeypatch.delenv("VV_AGENT", raising=False)
    monkeypatch.setenv("VV_CONFIG", str(tmp_path / "missing.toml"))

    result = runner.invoke(cli.app, ["--chat", "--agent", "codex"])
    assert result.exit_code == 0, result.output
    assert seen == {
        "agent": "codex", "bypass": True, "name": None, "notes": cli.notes.Pending(),
    }


def test_chat_flag_rejects_repo_url(monkeypatch, tmp_path):
    """`vv --chat <url>` must error out — the combination is contradictory."""
    monkeypatch.delenv("VV_AGENT", raising=False)
    monkeypatch.setenv("VV_CONFIG", str(tmp_path / "missing.toml"))
    # Sanity: neither downstream entry point should be reached.
    monkeypatch.setattr(
        cli, "_new_chat_session",
        lambda agent, bypass, name=None, pending_notes=None: pytest.fail("called"),
    )
    monkeypatch.setattr(
        cli, "_start_from_url",
        lambda url, agent, bypass, name=None, pending_notes=None: pytest.fail("called"),
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
        lambda url, agent, bypass, name=None, pending_notes=None: seen.update(
            url=url, agent=agent
        ),
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
        monkeypatch.setattr(cli, "_pick_github_repo", lambda _repos: picked)
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


# --- session notes: title + labels (`vv --title` / `vv --label`) ------------

@pytest.fixture
def notes_env(monkeypatch, tmp_path):
    """Three fake sessions on disk with the notes store pointed at tmp_path.

    Yields the worktree path of ``repo/alpha``; ``repo/bravo`` and the chat
    ``_chats/spark`` also exist, so targeting can be told apart.
    """
    monkeypatch.setenv("WORKTREES_DIR", str(tmp_path / "wt"))
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("VV_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.delenv("VV_AGENT", raising=False)
    monkeypatch.delenv("VV_REMOTE", raising=False)

    sessions = [
        ("repo", "alpha", tmp_path / "wt" / "repo" / "alpha"),
        ("repo", "bravo", tmp_path / "wt" / "repo" / "bravo"),
        (cli.CHATS, "spark", tmp_path / "wt" / "_chats" / "spark"),
    ]
    for _repo, _name, path in sessions:
        path.mkdir(parents=True)
    monkeypatch.setattr(cli, "_list_worktrees", lambda: sessions)
    return sessions[0][2]


def test_label_applies_to_the_session_the_cwd_is_inside(notes_env, monkeypatch):
    from vv import notes

    monkeypatch.chdir(notes_env)
    result = runner.invoke(cli.app, ["--label", "acme", "-l", "urgent"])
    assert result.exit_code == 0, result.output
    assert notes.for_session("repo", "alpha").labels == ["acme", "urgent"]
    assert "repo/alpha" in result.output and "acme, urgent" in result.output


def test_title_applies_to_the_session_the_cwd_is_inside(notes_env, monkeypatch):
    from vv import notes

    monkeypatch.chdir(notes_env)
    result = runner.invoke(cli.app, ["--title", "Acme onboarding"])
    assert result.exit_code == 0, result.output
    assert notes.for_session("repo", "alpha").title == "Acme onboarding"
    assert "repo/alpha" in result.output


def test_title_and_labels_can_be_set_in_one_go(notes_env, monkeypatch):
    from vv import notes

    monkeypatch.chdir(notes_env)
    result = runner.invoke(cli.app, ["-t", "Acme onboarding", "-l", "acme"])
    assert result.exit_code == 0, result.output
    note = notes.for_session("repo", "alpha")
    assert note.title == "Acme onboarding" and note.labels == ["acme"]


def test_setting_a_title_leaves_labels_alone_and_vice_versa(notes_env, monkeypatch):
    from vv import notes

    monkeypatch.chdir(notes_env)
    runner.invoke(cli.app, ["-l", "acme"])
    runner.invoke(cli.app, ["-t", "Acme onboarding"])       # labels survive
    assert notes.for_session("repo", "alpha").labels == ["acme"]
    runner.invoke(cli.app, ["-l", "urgent"])                # title survives
    note = notes.for_session("repo", "alpha")
    assert note.title == "Acme onboarding" and note.labels == ["acme", "urgent"]


def test_an_empty_title_clears_it(notes_env, monkeypatch):
    from vv import notes

    monkeypatch.chdir(notes_env)
    runner.invoke(cli.app, ["-t", "Acme onboarding", "-l", "acme"])
    result = runner.invoke(cli.app, ["--title", ""])
    assert result.exit_code == 0, result.output
    note = notes.for_session("repo", "alpha")
    assert note.title is None and note.labels == ["acme"]   # labels untouched
    assert "cleared" in result.output


def test_notes_work_from_a_subdirectory_of_the_session(notes_env, monkeypatch):
    from vv import notes

    nested = notes_env / "src" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert runner.invoke(cli.app, ["-l", "acme", "-t", "Deep work"]).exit_code == 0
    note = notes.for_session("repo", "alpha")
    assert note.labels == ["acme"] and note.title == "Deep work"


def test_label_removes_with_a_leading_minus(notes_env, monkeypatch):
    from vv import notes

    monkeypatch.chdir(notes_env)
    runner.invoke(cli.app, ["-l", "acme", "-l", "urgent"])
    result = runner.invoke(cli.app, ["-l", "-urgent"])
    assert result.exit_code == 0, result.output
    assert notes.for_session("repo", "alpha").labels == ["acme"]


def test_notes_can_target_another_session_by_name(notes_env, monkeypatch):
    from vv import notes

    monkeypatch.chdir(notes_env)                 # inside alpha…
    result = runner.invoke(cli.app, ["-l", "acme", "-t", "Scratch", "--name", "spark"])
    assert result.exit_code == 0, result.output
    spark = notes.for_session(cli.CHATS, "spark")
    assert spark.labels == ["acme"] and spark.title == "Scratch"   # …spark is annotated
    assert not notes.for_session("repo", "alpha")


def test_notes_outside_any_session_errors(notes_env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for args in (["-l", "acme"], ["-t", "Acme"]):
        result = runner.invoke(cli.app, args)
        assert result.exit_code == 1
        assert "not inside a vv session" in result.output


def test_notes_with_an_unknown_name_error(notes_env):
    result = runner.invoke(cli.app, ["-l", "acme", "--name", "nope"])
    assert result.exit_code == 1
    assert "no session named 'nope'" in result.output


def test_label_with_a_bare_sign_errors(notes_env):
    result = runner.invoke(cli.app, ["--label=-", "--name", "alpha"])
    assert result.exit_code == 1
    assert "not a label" in result.output


def test_notes_never_open_the_menu_or_a_session(notes_env, monkeypatch):
    monkeypatch.setattr(
        cli, "_interactive_menu", lambda *a, **k: pytest.fail("menu opened")
    )
    assert runner.invoke(cli.app, ["-l", "acme", "--name", "alpha"]).exit_code == 0
    assert runner.invoke(cli.app, ["-t", "Acme", "--name", "alpha"]).exit_code == 0


def test_notes_alongside_a_url_annotate_the_new_session(monkeypatch, tmp_path):
    """`vv <url> -t TEXT -l TAG` forwards the notes into the create flow."""
    from vv import notes

    seen: dict = {}
    monkeypatch.setattr(
        cli, "_start_from_url",
        lambda url, agent, bypass, name=None, pending_notes=None: seen.update(
            notes=pending_notes
        ),
    )
    monkeypatch.delenv("VV_AGENT", raising=False)
    monkeypatch.setenv("VV_CONFIG", str(tmp_path / "missing.toml"))
    result = runner.invoke(cli.app, ["-t", "Acme", "-l", "acme", "-l", "urgent", _REPO_URL])
    assert result.exit_code == 0, result.output
    assert seen["notes"] == notes.Pending(title="Acme", label_specs=("acme", "urgent"))


def test_a_new_session_records_its_notes(chat_env):
    from vv import notes

    cli._new_chat_session(
        "claude", bypass=False, name="otter",
        pending_notes=notes.Pending(title="Acme", label_specs=("acme",)),
    )
    note = notes.for_session(cli.CHATS, "otter")
    assert note.title == "Acme" and note.labels == ["acme"]


def test_a_bad_label_does_not_sink_a_created_session(chat_env, capsys):
    from vv import notes

    cli._new_chat_session(
        "claude", bypass=False, name="otter",
        pending_notes=notes.Pending(title="Acme", label_specs=("-",)),
    )
    assert (chat_env / "wt" / "_chats" / "otter").is_dir()   # session still created
    note = notes.for_session(cli.CHATS, "otter")
    assert note.title == "Acme" and note.labels == []        # title still applied
    assert "labels not applied" in capsys.readouterr().out


def test_deleting_a_session_forgets_its_notes(chat_env):
    from vv import notes

    cli._new_chat_session(
        "claude", bypass=False, name="otter",
        pending_notes=notes.Pending(title="Acme", label_specs=("acme",)),
    )
    cli._delete_chat("otter", chat_env / "wt" / "_chats" / "otter", live=set())
    assert notes.all_notes() == {}


def test_deleting_a_repo_forgets_its_sessions_notes(repo_delete_harness):
    from vv import notes

    notes.apply_labels("repo", "falcon", ["acme"])
    notes.set_title("other", "gamma", "keep me")
    repo_delete_harness(worktrees=["falcon"])
    cli._delete_repo("repo")
    assert list(notes.all_notes()) == ["other/gamma"]


def test_remote_mode_forwards_title_and_label_specs(remote_harness):
    seen, write_config = remote_harness
    write_config('[remote]\nhost = "h"\n')
    result = runner.invoke(
        cli.app, ["--remote", "-t", "Acme", "-l", "acme", "--label=-old", _REPO_URL]
    )
    assert result.exit_code == 0, result.output
    # "=" form so the remote's parser can't read a removal's '-' as a flag
    assert seen["argv"] == [
        "--name", "otter", "--local",
        "--title=Acme", "--label=acme", "--label=-old", _REPO_URL,
    ]


def test_annotating_an_existing_session_stays_local_in_remote_mode(
    remote_harness, notes_env, monkeypatch
):
    """`vv -l TAG` / `-t TEXT` is local bookkeeping — it must not open a cmux tab."""
    from vv import notes

    seen, write_config = remote_harness
    write_config('mode = "remote"\n[remote]\nhost = "h"\n')
    monkeypatch.chdir(notes_env)
    result = runner.invoke(cli.app, ["-l", "acme", "-t", "Acme"])
    assert result.exit_code == 0, result.output
    assert seen == {}                                   # no remote launch
    note = notes.for_session("repo", "alpha")
    assert note.labels == ["acme"] and note.title == "Acme"


# --- session summary caching ------------------------------------------------

def test_session_summaries_only_regenerates_changed_sessions(monkeypatch, tmp_path):
    from vv import config, summary

    worktrees = [
        ("repo", "alpha", tmp_path / "alpha"),
        ("repo", "bravo", tmp_path / "bravo"),
    ]
    monkeypatch.setattr(config, "configured_summary_agent", lambda: "claude")

    # In-memory cache that persists across calls (mimics the on-disk file).
    written = {}
    monkeypatch.setattr(summary, "save_cache", lambda c: (written.clear(), written.update(c)))
    monkeypatch.setattr(summary, "load_cache", lambda: dict(written))

    fps = {tmp_path / "alpha": "fp-a1", tmp_path / "bravo": "fp-b1"}
    monkeypatch.setattr(summary, "session_fingerprint", lambda p: fps[p])

    generated_for = []

    def fake_summarize_all(agent, stale):
        generated_for.append(sorted(k for k in stale))
        return {key: f"summary-of-{key[1]}" for key in stale}

    monkeypatch.setattr(summary, "summarize_all", fake_summarize_all)

    # First open: both are new -> both generated.
    res1 = cli._session_summaries("claude", worktrees)
    assert res1 == {("repo", "alpha"): "summary-of-alpha", ("repo", "bravo"): "summary-of-bravo"}
    assert generated_for[-1] == [("repo", "alpha"), ("repo", "bravo")]

    # Second open, nothing changed -> nothing regenerated, results served from cache.
    res2 = cli._session_summaries("claude", worktrees)
    assert res2 == res1
    assert generated_for[-1] == [("repo", "alpha"), ("repo", "bravo")]  # unchanged -> no new call
    assert len(generated_for) == 1  # summarize_all not called a second time

    # bravo is "opened" (fingerprint changes) -> only bravo regenerates.
    fps[tmp_path / "bravo"] = "fp-b2"
    res3 = cli._session_summaries("claude", worktrees)
    assert generated_for[-1] == [("repo", "bravo")]
    assert res3[("repo", "alpha")] == "summary-of-alpha"  # alpha still cached


def test_session_summaries_prunes_deleted_sessions_from_cache(monkeypatch, tmp_path):
    from vv import config, summary

    monkeypatch.setattr(config, "configured_summary_agent", lambda: "claude")
    written = {}
    monkeypatch.setattr(summary, "save_cache", lambda c: (written.clear(), written.update(c)))
    monkeypatch.setattr(summary, "load_cache", lambda: dict(written))
    monkeypatch.setattr(summary, "session_fingerprint", lambda p: "fp")
    monkeypatch.setattr(
        summary, "summarize_all",
        lambda agent, stale: {key: "s" for key in stale},
    )

    cli._session_summaries("claude", [("r", "a", tmp_path / "a"), ("r", "b", tmp_path / "b")])
    assert set(written) == {"r/a", "r/b"}

    # 'b' is gone on the next open -> cache no longer carries it.
    cli._session_summaries("claude", [("r", "a", tmp_path / "a")])
    assert set(written) == {"r/a"}


# --- session cards ----------------------------------------------------------

def _plain(rows):
    """Flatten card rows to plain lines (dropping styles) for assertions."""
    return ["".join(text for _style, text in row) for row in rows]


def _git_card(**over):
    card = {
        "running": False, "summary": "Refactoring the auth flow",
        "branch": "breezy", "folder": "repo/breezy", "pr": None, "when": "2h ago",
    }
    card.update(over)
    return card


def test_card_lines_layout_and_uniform_width():
    rows = _plain(cli._card_lines(_git_card(), width=54))
    # bordered box: top, summary, branch/folder, pr, bottom
    assert rows[0].startswith("╭") and rows[0].endswith("╮")
    assert rows[-1].startswith("╰") and rows[-1].endswith("╯")
    assert all(len(r) == len(rows[0]) for r in rows)          # every row same width
    assert "▸ " not in rows[1] and "▹ Refactoring the auth flow" in rows[1]  # idle triangle
    # branch reads fully (no leading glyph to clip it), then " · " then the folder
    assert "breezy · repo/breezy" in rows[2]
    assert "⎇" not in rows[2]
    assert "2h ago" in rows[3]                                 # timestamp right-aligned


def test_card_lines_show_a_user_title_above_the_summary():
    rows = _plain(cli._card_lines(_git_card(title="Acme onboarding"), width=54))
    assert "▹ Acme onboarding" in rows[1]                  # the title is the headline…
    assert "Refactoring the auth flow" in rows[2]          # …and the summary stays
    assert rows[2].startswith("│   ")                      # indented under the title
    assert "breezy · repo/breezy" in rows[3]
    assert all(len(r) == len(rows[0]) for r in rows)


def test_card_title_and_summary_are_styled_apart():
    rows = cli._card_lines(_git_card(title="Acme onboarding"), width=54)
    styles = {text.strip(): style for row in rows for style, text in row if text.strip()}
    assert styles["Acme onboarding"] == "class:card.title"
    assert styles["Refactoring the auth flow"] == "class:card.summary"


def test_card_without_a_title_renders_the_summary_as_the_headline():
    # Unchanged from before titles existed: dot + summary, in the title style.
    rows = cli._card_lines(_git_card(), width=54)
    assert ("class:card.title", "Refactoring the auth flow") in rows[1]
    assert len(rows) == 5                                  # no extra row added


def test_card_with_a_title_but_no_summary_shows_only_the_title():
    rows = _plain(cli._card_lines(_git_card(title="Acme onboarding", summary=None), width=54))
    assert "▹ Acme onboarding" in rows[1]
    assert len(rows) == 5                                  # no empty summary row
    assert "breezy · repo/breezy" in rows[2]


def test_card_lines_show_labels_under_the_title():
    rows = _plain(cli._card_lines(_git_card(labels=["Big Customer", "urgent"]), width=54))
    assert "Refactoring the auth flow" in rows[1]
    assert "#Big Customer  #urgent" in rows[2]      # labels sit directly under the title
    assert rows[2].startswith("│   #")              # indented to line up with the title
    assert "breezy · repo/breezy" in rows[3]        # branch line pushed down
    assert all(len(r) == len(rows[0]) for r in rows)


def test_card_lines_omit_the_label_row_when_there_are_none():
    # A card with no labels (or the key absent entirely) renders as before.
    for card in (_git_card(), _git_card(labels=[])):
        rows = _plain(cli._card_lines(card, width=54))
        assert len(rows) == 5 and "breezy · repo/breezy" in rows[2]


def test_card_lines_wrap_a_long_label_row():
    rows = _plain(cli._card_lines(_git_card(labels=[f"label-{i}" for i in range(8)]), width=54))
    assert "#label-0" in rows[2] and "#label-7" in rows[3]   # wrapped onto a second row
    assert all(len(r) == len(rows[0]) for r in rows)         # still uniform width


def test_card_lines_branch_shows_dirty_asterisk():
    clean = _plain(cli._card_lines(_git_card(dirty=False), width=54))[2]
    dirty = _plain(cli._card_lines(_git_card(dirty=True), width=54))[2]
    assert "breezy · repo/breezy" in clean and "breezy✱" not in clean
    assert "breezy✱ · repo/breezy" in dirty  # ✱ sits between branch and separator


def test_card_lines_running_dot_and_chat_without_branch():
    running = _plain(cli._card_lines(_git_card(running=True), width=54))
    assert "▸ Refactoring the auth flow" in running[1]          # filled triangle when live

    chat = _plain(cli._card_lines(
        _git_card(branch=None, folder="_chats/spark", summary="Scratch space"), width=54
    ))
    assert "_chats/spark" in chat[2]                            # chats: just the folder
    assert "chat session" in chat[3]


def test_card_pr_segment_states():
    # Open PR: outline circle + check mark, colored by the check rollup.
    assert cli._pr_segment(_git_card(pr={"number": 3, "state": "open", "checks": "failing"})) \
        == ("○ PR #3 ✗ failing", "class:card.pr.fail")
    assert cli._pr_segment(_git_card(pr={"number": 5, "state": "open", "checks": "passing"})) \
        == ("○ PR #5 ✓ passing", "class:card.pr.pass")
    assert cli._pr_segment(_git_card(pr={"number": 8, "state": "open", "checks": "pending"})) \
        == ("○ PR #8 ◔ pending", "class:card.pr.pending")
    assert cli._pr_segment(_git_card(pr={"number": 9, "state": "open", "checks": None})) \
        == ("○ PR #9 open", "class:card.pr.open")
    # Draft / merged / closed: their own circle-family glyph, no check overlay.
    assert cli._pr_segment(_git_card(pr={"number": 7, "state": "draft"})) \
        == ("◌ PR #7 draft", "class:card.pr.draft")
    assert cli._pr_segment(_git_card(pr={"number": 2, "state": "merged"})) \
        == ("● PR #2 merged", "class:card.pr.merged")
    assert cli._pr_segment(_git_card(pr={"number": 4, "state": "closed"})) \
        == ("⊘ PR #4 closed", "class:card.pr.closed")
    assert cli._pr_segment(_git_card(pr=None))[0] == "○ no open PR"
    assert cli._pr_segment(_git_card(branch=None))[0] == "❝ chat session"


def test_pr_segment_shows_checking_while_refresh_pending():
    # A git session whose PR status hasn't been fetched yet shows a placeholder,
    # not "no open PR" (which would be wrong until the background fetch lands).
    assert cli._pr_segment(_git_card(pr=None, pr_pending=True))[0] == "⋯ checking…"
    # Once the fetch lands with no PR, it settles to the real answer.
    assert cli._pr_segment(_git_card(pr=None, pr_pending=False))[0] == "○ no open PR"


def test_render_cards_marks_only_the_selected_card():
    cards = [_git_card(folder="r/a"), _git_card(folder="r/b")]
    tokens = cli._render_cards(cards, pointed_at=1, width=54)
    text = "".join(t for _s, t in tokens)
    assert "❯ " in text and "▌ " in text                        # selection bar present
    # the SetCursorPosition sentinel is emitted once, for the selected card
    assert [s for s, _t in tokens].count("[SetCursorPosition]") == 1


def test_selected_card_has_no_unwashed_gaps():
    # Render ONLY the selected card: every visible segment (text, padding, gutter)
    # must carry card.sel, so the highlight has no dark holes on short lines.
    tokens = cli._render_cards([_git_card(summary="x")], pointed_at=0, width=54)
    unwashed = [(s, t) for s, t in tokens if t not in ("", "\n") and "card.sel" not in s]
    assert unwashed == []


def test_unselected_card_has_no_highlight():
    tokens = cli._render_cards([_git_card(), _git_card()], pointed_at=0, width=54)
    # card at index 1 is not selected -> none of the "r/b"-region segments washed.
    # Simplest: with pointed_at far out of range, nothing is washed at all.
    none_sel = cli._render_cards([_git_card()], pointed_at=99, width=54)
    assert all("card.sel" not in s for s, _t in none_sel)


def test_keep_card_visible_sets_scroll_offsets():
    import questionary
    from prompt_toolkit.layout.containers import ScrollOffsets

    cards = [_git_card(summary="a\nb\nc"), _git_card()]
    choices = [questionary.Choice(title="a", value=1), questionary.Choice(title="b", value=2)]
    q = questionary.select("m", choices=choices)
    cli._keep_card_visible(q, cards, width=54)

    def choice_window():
        for c in q.application.layout.walk():
            if type(getattr(c, "content", None)).__name__ == "InquirerControl":
                return c
        return None

    off = choice_window().scroll_offsets
    assert isinstance(off, ScrollOffsets)
    assert off.top > 0 and off.bottom > 0     # a margin is reserved on both sides


def test_card_theme_merges_config_over_defaults(monkeypatch):
    from vv import config

    monkeypatch.setattr(config, "configured_card_glyphs", lambda: {"running": ">", "chat": "🗨"})
    monkeypatch.setattr(config, "configured_card_colors", lambda: {"branch": "#ff8800"})
    theme = cli._card_theme()
    assert theme.glyphs["running"] == ">"          # overridden
    assert theme.glyphs["idle"] == cli._DEFAULT_GLYPHS["idle"]   # untouched default
    assert theme.colors["branch"] == "#ff8800"     # overridden
    assert theme.colors["pr_fail"] == cli._DEFAULT_COLORS["pr_fail"]


def test_card_theme_drops_malformed_colors(monkeypatch):
    from vv import config

    monkeypatch.setattr(config, "configured_card_glyphs", lambda: {})
    monkeypatch.setattr(config, "configured_card_colors", lambda: {"branch": "notacolor"})
    # a color prompt_toolkit can't parse is dropped, keeping the default (so the
    # menu can't crash at render time on a config typo)
    assert cli._card_theme().colors["branch"] == cli._DEFAULT_COLORS["branch"]


def test_render_honors_theme_glyph_overrides():
    theme = cli.CardTheme(
        glyphs={**cli._DEFAULT_GLYPHS, "running": "»", "check_failing": "X", "select_pointer": "="},
        colors=cli._DEFAULT_COLORS,
    )
    card = _git_card(running=True, pr={"number": 3, "state": "open", "checks": "failing"})
    text = "".join(t for _s, t in cli._render_cards([card], pointed_at=0, width=54, theme=theme))
    assert "» " in text and "X failing" in text and "= " in text  # all three overrides applied
    assert "▸ " not in text and "❯ " not in text                  # defaults replaced


def test_relative_time_buckets(monkeypatch):
    import time as _time
    monkeypatch.setattr(_time, "time", lambda: 1_000_000.0)
    assert cli._relative_time(1_000_000.0 - 30) == "just now"
    assert cli._relative_time(1_000_000.0 - 5 * 60) == "5m ago"
    assert cli._relative_time(1_000_000.0 - 3 * 3600) == "3h ago"
    assert cli._relative_time(1_000_000.0 - 2 * 86400) == "2d ago"
    assert cli._relative_time(1_000_000.0 - 3 * 604800) == "3w ago"


def test_wrap_choice_lines_enables_wrapping_on_the_choices_window():
    import questionary

    q = questionary.select("Which?", choices=[
        questionary.Choice(title="a\n   long summary line", value=1),
        questionary.Choice(title="b", value=2),
    ])

    def choice_window():
        for c in q.application.layout.walk():
            if type(getattr(c, "content", None)).__name__ == "InquirerControl":
                return c
        return None

    window = choice_window()
    assert bool(window.wrap_lines()) is False   # questionary's default
    cli._wrap_choice_lines(q)
    assert bool(window.wrap_lines()) is True     # flipped on
