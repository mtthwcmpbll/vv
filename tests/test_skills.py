"""Tests for the bundled-skill installer (`vv --skills`)."""

from __future__ import annotations

import pytest

from vv import cli, skills


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    """Point `Path.home()` at a throwaway dir, with no agent tools present."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home


@pytest.fixture
def bundled(monkeypatch, tmp_path):
    """Replace vv's shipped skills with a controllable one-skill directory."""
    root = tmp_path / "bundled"
    skill = root / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\n---\nbody\n")
    monkeypatch.setattr(skills, "BUNDLED_DIR", root)
    return skill


# --- discovery ---------------------------------------------------------------


def test_targets_cover_every_supported_tool(fake_home):
    assert [t.key for t in skills.targets()] == [
        "claude", "cursor", "copilot", "codex", "antigravity",
    ]


def test_targets_are_rooted_in_the_current_home(fake_home):
    by_key = {t.key: t.root for t in skills.targets()}
    assert by_key["claude"] == fake_home / ".claude"
    assert by_key["antigravity"] == fake_home / ".gemini" / "config"


@pytest.mark.parametrize(
    ("env_var", "key"),
    [("CODEX_HOME", "codex"), ("CLAUDE_CONFIG_DIR", "claude")],
)
def test_env_var_overrides_a_root(fake_home, monkeypatch, tmp_path, env_var, key):
    monkeypatch.setenv(env_var, str(tmp_path / "elsewhere"))
    root = next(t.root for t in skills.targets() if t.key == key)
    assert root == tmp_path / "elsewhere"


def test_discovered_only_returns_tools_whose_root_exists(fake_home):
    assert skills.discovered() == ()
    (fake_home / ".cursor").mkdir()
    (fake_home / ".codex").mkdir()
    assert [t.key for t in skills.discovered()] == ["cursor", "codex"]


def test_a_root_that_is_a_file_is_not_discovered(fake_home):
    (fake_home / ".cursor").write_text("not a directory")
    assert skills.discovered() == ()


def test_bundled_skills_lists_the_shipped_pr_skill():
    # Against the real package data, not the fixture — vv must actually ship it.
    assert "pr" in skills.bundled_skills()
    assert (skills.BUNDLED_DIR / "pr" / "SKILL.md").is_file()


def test_bundled_skills_ignores_directories_without_a_skill_file(bundled, monkeypatch):
    (bundled.parent / "not-a-skill").mkdir()
    assert skills.bundled_skills() == ("demo",)


# --- status + install --------------------------------------------------------


def _target(home, key="cursor", rel=".cursor"):
    root = home / rel
    root.mkdir(parents=True, exist_ok=True)
    return skills.Target(key, key.title(), root)


def test_status_is_missing_then_same_after_install(fake_home, bundled):
    target = _target(fake_home)
    assert skills.status("demo", target) == "missing"
    skills.install("demo", target)
    assert skills.status("demo", target) == "same"


def test_status_is_differs_when_edited_in_place(fake_home, bundled):
    target = _target(fake_home)
    skills.install("demo", target)
    (target.path_for("demo") / "SKILL.md").write_text("edited by hand\n")
    assert skills.status("demo", target) == "differs"


def test_status_notices_a_changed_supporting_file(fake_home, bundled):
    # Skills may carry scripts/ and references/ — the whole tree must be compared.
    (bundled / "scripts").mkdir()
    (bundled / "scripts" / "run.sh").write_text("echo hi\n")
    target = _target(fake_home)
    skills.install("demo", target)
    assert skills.status("demo", target) == "same"
    (target.path_for("demo") / "scripts" / "run.sh").write_text("echo bye\n")
    assert skills.status("demo", target) == "differs"


def test_install_creates_the_skills_dir_under_an_existing_root(fake_home, bundled):
    target = _target(fake_home)
    path = skills.install("demo", target)
    assert path == fake_home / ".cursor" / "skills" / "demo"
    assert (path / "SKILL.md").read_text() == "---\nname: demo\n---\nbody\n"


def test_install_copies_supporting_files(fake_home, bundled):
    (bundled / "references").mkdir()
    (bundled / "references" / "notes.md").write_text("detail\n")
    path = skills.install("demo", _target(fake_home))
    assert (path / "references" / "notes.md").read_text() == "detail\n"


def test_install_drops_files_the_skill_no_longer_ships(fake_home, bundled):
    target = _target(fake_home)
    skills.install("demo", target)
    stale = target.path_for("demo") / "gone.md"
    stale.write_text("from an older vv\n")
    skills.install("demo", target)
    assert not stale.exists()          # replaced, not merged
    assert skills.status("demo", target) == "same"


def test_install_rejects_an_unknown_skill(fake_home, bundled):
    with pytest.raises(FileNotFoundError):
        skills.install("nope", _target(fake_home))


# --- the CLI flow ------------------------------------------------------------


@pytest.fixture
def install_cli(monkeypatch, fake_home, bundled):
    """Run `_install_skills()` against a fake home, capturing its output."""
    lines: list[str] = []
    confirms: list[str] = []
    monkeypatch.setattr(
        cli.typer, "secho", lambda msg, **kw: lines.append(str(msg))
    )

    def run(tools=(), *, confirm=True):
        for rel in tools:
            (fake_home / rel).mkdir(parents=True, exist_ok=True)

        class _Answer:
            def ask(self_inner):
                return confirm

        def fake_confirm(message, **kwargs):
            confirms.append(message)
            return _Answer()

        monkeypatch.setattr(cli.questionary, "confirm", fake_confirm)
        lines.clear()
        cli._install_skills()
        return lines, confirms

    return run


def test_cli_reports_when_no_tools_are_installed(install_cli):
    lines, _ = install_cli()
    assert "No supported agent tools found on this machine." in lines[0]
    assert any("looked in" in line for line in lines)


def test_cli_installs_into_every_discovered_tool(install_cli, fake_home):
    lines, _ = install_cli([".claude", ".cursor", ".gemini/config"])
    assert (fake_home / ".claude" / "skills" / "demo" / "SKILL.md").is_file()
    assert (fake_home / ".cursor" / "skills" / "demo" / "SKILL.md").is_file()
    assert (fake_home / ".gemini" / "config" / "skills" / "demo" / "SKILL.md").is_file()
    assert any("3 skill install(s)" in line for line in lines)


def test_cli_never_creates_a_root_for_an_absent_tool(install_cli, fake_home):
    install_cli([".cursor"])
    assert not (fake_home / ".codex").exists()
    assert not (fake_home / ".copilot").exists()


def test_cli_lists_the_tools_it_skipped(install_cli):
    lines, _ = install_cli([".cursor"])
    skipped = next(line for line in lines if line.startswith("Not installed here:"))
    assert "Codex" in skipped and "GitHub Copilot" in skipped


def test_cli_is_idempotent(install_cli, fake_home):
    install_cli([".cursor"])
    lines, confirms = install_cli([".cursor"])
    assert any("already up to date" in line for line in lines)
    assert any("0 skill install(s)" in line for line in lines)
    assert confirms == []               # nothing changed -> nothing to confirm


def test_cli_confirms_before_overwriting_a_modified_skill(install_cli, fake_home):
    install_cli([".cursor"])
    installed = fake_home / ".cursor" / "skills" / "demo" / "SKILL.md"
    installed.write_text("hand-edited\n")

    lines, confirms = install_cli([".cursor"], confirm=True)
    assert len(confirms) == 1
    assert installed.read_text() != "hand-edited\n"     # overwritten
    assert any("updated" in line for line in lines)


def test_cli_keeps_a_modified_skill_when_the_confirm_is_declined(install_cli, fake_home):
    install_cli([".cursor"])
    installed = fake_home / ".cursor" / "skills" / "demo" / "SKILL.md"
    installed.write_text("hand-edited\n")

    lines, confirms = install_cli([".cursor"], confirm=False)
    assert len(confirms) == 1
    assert installed.read_text() == "hand-edited\n"     # left alone
    assert any("kept as-is" in line for line in lines)


def test_declining_still_installs_where_the_skill_is_missing(install_cli, fake_home):
    install_cli([".cursor"])
    (fake_home / ".cursor" / "skills" / "demo" / "SKILL.md").write_text("edited\n")

    install_cli([".cursor", ".codex"], confirm=False)
    # The conflicted tool is kept, the fresh one still gets the skill.
    assert (fake_home / ".cursor" / "skills" / "demo" / "SKILL.md").read_text() == "edited\n"
    assert (fake_home / ".codex" / "skills" / "demo" / "SKILL.md").is_file()


def test_cli_isolates_a_failing_install(install_cli, monkeypatch, fake_home):
    real_install = skills.install

    def flaky(skill, target):
        if target.key == "cursor":
            raise OSError("read-only file system")
        return real_install(skill, target)

    monkeypatch.setattr(cli.skills, "install", flaky)
    lines, _ = install_cli([".cursor", ".codex"])
    assert any("failed" in line for line in lines)
    assert (fake_home / ".codex" / "skills" / "demo").is_dir()   # batch continued
    assert any("1 skill install(s)" in line for line in lines)
