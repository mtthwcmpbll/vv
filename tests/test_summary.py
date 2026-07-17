"""Tests for session summary generation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from vv import summary


# --- can_summarize ----------------------------------------------------------

def test_can_summarize_knows_claude():
    assert summary.can_summarize("claude") is True
    assert summary.can_summarize("claude --resume") is True


def test_can_summarize_false_for_unknown_command():
    assert summary.can_summarize("agy") is False
    assert summary.can_summarize("madeup-cli") is False


# --- _first_line ------------------------------------------------------------

def test_first_line_skips_blanks_and_strips():
    assert summary._first_line("\n\n  hello there  \nsecond\n") == "hello there"


def test_first_line_none_for_empty():
    assert summary._first_line("   \n\n") is None


# --- context gathering ------------------------------------------------------

def _worktree(remote_repo: Path, git, tmp_path: Path) -> Path:
    """A worktree branched off ``remote_repo`` with an unpushed commit + change."""
    clone = tmp_path / "clone"
    git("clone", "-q", str(remote_repo), str(clone), cwd=tmp_path)
    wt = tmp_path / "wt"
    git("worktree", "add", "-b", "feature", str(wt), "HEAD", cwd=clone)
    (wt / "new.txt").write_text("work\n")
    git("add", "-A", cwd=wt)
    git("commit", "-q", "-m", "add new.txt", cwd=wt)
    (wt / "dirty.txt").write_text("uncommitted\n")  # leave an unstaged change
    return wt


def test_git_context_includes_branch_commits_and_changes(remote_repo, git, tmp_path):
    wt = _worktree(remote_repo, git, tmp_path)
    context = summary._gather_context(wt)
    assert "Branch: feature" in context
    assert "add new.txt" in context      # the session commit
    assert "dirty.txt" in context        # the uncommitted change


def test_dir_context_lists_chat_files(tmp_path):
    chat = tmp_path / "chat"
    chat.mkdir()
    (chat / "notes.md").write_text("hi\n")
    context = summary._gather_context(chat)
    assert "notes.md" in context


def test_dir_context_empty_dir_yields_nothing(tmp_path):
    chat = tmp_path / "chat"
    chat.mkdir()
    assert summary._gather_context(chat) == ""


# --- transcript sourcing ----------------------------------------------------

def test_encode_project_path_matches_claude_layout():
    # Every non-alphanumeric char collapses to '-' (slashes, dots, underscores).
    assert summary._encode_project_path(Path("/home/matt/.vv/worktrees/_chats/fern")) == (
        "-home-matt--vv-worktrees--chats-fern"
    )


@pytest.fixture
def stores(monkeypatch, tmp_path):
    """Isolate all three agent transcript stores under tmp so tests are hermetic.

    Returns a namespace with ``write_claude`` / ``write_gemini`` / ``write_codex``
    helpers keyed on a session path.
    """
    claude = tmp_path / "claude"
    gemini_tmp = tmp_path / "gemini_tmp"
    gemini_projects = tmp_path / "gemini_projects.json"
    codex = tmp_path / "codex"
    monkeypatch.setattr(summary, "CLAUDE_PROJECTS_DIR", claude)
    monkeypatch.setattr(summary, "GEMINI_TMP_DIR", gemini_tmp)
    monkeypatch.setattr(summary, "GEMINI_PROJECTS_FILE", gemini_projects)
    monkeypatch.setattr(summary, "CODEX_SESSIONS_DIR", codex)
    # Keep the scratch cwd + summary cache out of the real ~/.vv.
    monkeypatch.setenv("WORKTREES_DIR", str(tmp_path / "wt_root"))

    class Stores:
        def write_claude(self, session: Path, lines: list[dict]) -> None:
            d = claude / summary._encode_project_path(session)
            d.mkdir(parents=True, exist_ok=True)
            (d / "sess.jsonl").write_text("\n".join(json.dumps(o) for o in lines) + "\n")

        def write_gemini(self, session: Path, messages: list[dict]) -> None:
            tag = session.name
            gemini_projects.write_text(json.dumps({"projects": {str(session): tag}}))
            chats = gemini_tmp / tag / "chats"
            chats.mkdir(parents=True, exist_ok=True)
            (chats / "session-x.json").write_text(json.dumps({"messages": messages}))

        def write_codex(self, session: Path, events: list[dict]) -> None:
            day = codex / "2026" / "07" / "17"
            day.mkdir(parents=True, exist_ok=True)
            meta = {"type": "session_meta", "payload": {"cwd": str(session)}}
            lines = [meta, *events]
            (day / "rollout-x.jsonl").write_text("\n".join(json.dumps(o) for o in lines) + "\n")

    return Stores()


def _cl_user(text):
    return {"type": "user", "message": {"content": text}}


def _cl_assistant(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def test_claude_transcript_extracts_intent_and_recent(stores, tmp_path):
    session = tmp_path / "wt"
    session.mkdir()
    stores.write_claude(session, [
        _cl_user("Add a dark mode toggle to settings"),        # the task
        {"type": "assistant", "message": {"content": [         # tool call: dropped
            {"type": "tool_use", "name": "Edit", "input": {}}]}},
        _cl_user("<bash-input>ls</bash-input>"),               # synthetic: dropped
        _cl_user("Now also persist the choice to localStorage"),
        _cl_assistant("Persisting the toggle now."),
    ])
    context = summary._transcript_context(session)
    assert "Add a dark mode toggle" in context           # first message kept
    assert "persist the choice to localStorage" in context
    assert "Persisting the toggle now" in context
    assert "tool_use" not in context and "bash-input" not in context


def test_gemini_transcript_extracts_user_and_model_turns(stores, tmp_path):
    session = tmp_path / "papaya"
    session.mkdir()
    stores.write_gemini(session, [
        {"type": "user", "content": [{"text": "Wire up the export button"}]},
        {"type": "info", "content": "Update successful!"},          # not conversation
        {"type": "gemini", "content": "Wiring the button to exportCSV()."},
    ])
    context = summary._transcript_context(session)
    assert "user: Wire up the export button" in context
    assert "assistant: Wiring the button to exportCSV()." in context
    assert "Update successful" not in context             # info turn dropped


def test_codex_transcript_extracts_message_events(stores, tmp_path):
    session = tmp_path / "maple"
    session.mkdir()
    stores.write_codex(session, [
        {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                              "content": [{"text": "SYSTEM PROMPT"}]}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Fix the flaky test"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "Found the race; patching."}},
    ])
    context = summary._transcript_context(session)
    assert "user: Fix the flaky test" in context
    assert "assistant: Found the race; patching." in context
    assert "SYSTEM PROMPT" not in context                 # response_item noise dropped


def test_transcript_probes_claude_first_then_falls_through(stores, tmp_path):
    session = tmp_path / "wt"
    session.mkdir()
    # Gemini has a real transcript; Claude does not -> Gemini is used.
    stores.write_gemini(session, [{"type": "user", "content": "gemini-side task"}])
    assert "gemini-side task" in summary._transcript_context(session)

    # Now add a Claude transcript for the same session: Claude wins, short-circuit.
    stores.write_claude(session, [_cl_user("claude-side task")])
    context = summary._transcript_context(session)
    assert "claude-side task" in context
    assert "gemini-side task" not in context


def test_transcript_context_empty_without_any_transcript(stores, tmp_path):
    assert summary._transcript_context(tmp_path / "nope") == ""


def test_claude_provider_skips_vvs_own_summary_runs(stores, tmp_path):
    # A real session transcript, then a NEWER transcript that is one of vv's own
    # summary runs (first user turn is the summary prompt). The summary must read
    # the real session, not the summary-of-a-summary.
    session = tmp_path / "wt"
    session.mkdir()
    project = tmp_path / "claude" / summary._encode_project_path(session)
    project.mkdir(parents=True)
    real = project / "real.jsonl"
    real.write_text(json.dumps(_cl_user("Build the OAuth callback handler")) + "\n")
    ours = project / "summary-run.jsonl"
    ours.write_text(json.dumps(_cl_user(summary._PROMPT + "...context...")) + "\n")
    import os
    os.utime(ours, ns=(9_000_000_000_000_000_000, 9_000_000_000_000_000_000))  # newer

    assert summary._claude_file(session) == real          # picks the real one
    assert summary._transcript_path(session) == real      # fingerprint uses it too
    context = summary._transcript_context(session)
    assert "Build the OAuth callback handler" in context
    assert "Summarize in one terse line" not in context


def test_gather_context_combines_git_and_transcript(stores, remote_repo, git, tmp_path):
    wt = _worktree(remote_repo, git, tmp_path)
    stores.write_claude(wt, [_cl_user("Implement CSV export for reports")])
    context = summary._gather_context(wt)
    assert "Branch: feature" in context                  # git source
    assert "Recent agent conversation:" in context       # transcript source
    assert "Implement CSV export for reports" in context


def test_transcript_alone_makes_an_uncommitted_session_summarizable(stores, monkeypatch, tmp_path):
    # No git, empty dir: the ONLY signal is the transcript — the case the user
    # cares about (nothing committed yet). summarize() must still run the agent.
    session = tmp_path / "chat"
    session.mkdir()
    stores.write_codex(session, [
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Brainstorm launch names"}},
    ])

    def fake_run(argv, **kwargs):
        assert "Brainstorm launch names" in argv[-1]
        return subprocess.CompletedProcess(argv, 0, stdout="Brainstorming launch names\n", stderr="")

    monkeypatch.setattr(summary.subprocess, "run", fake_run)
    assert summary.summarize("claude", session) == "Brainstorming launch names"


# --- summarize --------------------------------------------------------------

def test_summarize_returns_none_for_unrunnable_agent(tmp_path):
    (tmp_path / ".git").write_text("gitdir: /nope\n")
    assert summary.summarize("agy", tmp_path) is None


def test_summarize_returns_none_without_context(stores, tmp_path):
    # No .git and an empty dir -> nothing to summarize, agent never runs.
    assert summary.summarize("claude", tmp_path) is None


def test_summarize_runs_agent_and_returns_first_line(stores, monkeypatch, tmp_path):
    (tmp_path / "notes.md").write_text("hi\n")  # gives non-empty dir context
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(argv, 0, stdout="Refactoring auth flow\nextra\n", stderr="")

    monkeypatch.setattr(summary.subprocess, "run", fake_run)
    result = summary.summarize("claude", tmp_path)
    assert result == "Refactoring auth flow"
    # claude runs in print mode, with the prompt last.
    assert captured["argv"][:2] == ["claude", "-p"]
    # It runs in an isolated scratch dir, NOT the session (would pollute history).
    assert captured["cwd"].endswith(".summary-scratch")
    assert captured["cwd"] != str(tmp_path)
    assert summary._PROMPT.strip().split("\n")[0] in captured["argv"][-1]


def test_summarize_returns_none_on_failure(stores, monkeypatch, tmp_path):
    (tmp_path / "notes.md").write_text("hi\n")

    def boom(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, summary.TIMEOUT_SECONDS)

    monkeypatch.setattr(summary.subprocess, "run", boom)
    assert summary.summarize("claude", tmp_path) is None


# --- summarize_all ----------------------------------------------------------

def test_summarize_all_maps_keys_and_omits_failures(monkeypatch, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for p in (a, b):
        p.mkdir()
        (p / "f").write_text("x\n")

    def fake_summarize(agent, path):
        return "summary-of-a" if path == a else None

    monkeypatch.setattr(summary, "summarize", fake_summarize)
    result = summary.summarize_all("claude", {("r", "a"): a, ("r", "b"): b})
    assert result == {("r", "a"): "summary-of-a"}


def test_summarize_all_empty_when_agent_cannot_run(tmp_path):
    assert summary.summarize_all("agy", {("r", "a"): tmp_path}) == {}


# --- caching ----------------------------------------------------------------

def test_session_fingerprint_changes_when_transcript_grows(stores, tmp_path):
    session = tmp_path / "wt"
    session.mkdir()
    stores.write_claude(session, [_cl_user("first prompt")])
    before = summary.session_fingerprint(session)

    # A new turn rewrites the transcript with a newer mtime -> new fingerprint.
    import os
    transcript = summary._claude_file(session)
    os.utime(transcript, ns=(2_000_000_000_000_000_000, 2_000_000_000_000_000_000))
    after = summary.session_fingerprint(session)
    assert before != after


def test_session_fingerprint_stable_without_activity(stores, tmp_path):
    session = tmp_path / "wt"
    session.mkdir()
    stores.write_claude(session, [_cl_user("hello")])
    assert summary.session_fingerprint(session) == summary.session_fingerprint(session)


def test_cache_round_trips(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKTREES_DIR", str(tmp_path / "wt"))
    assert summary.load_cache() == {}  # nothing yet
    payload = {"repo/name": {"agent": "claude", "fingerprint": "fp", "summary": "hi"}}
    summary.save_cache(payload)
    assert summary.load_cache() == payload


def test_cache_ignored_on_version_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKTREES_DIR", str(tmp_path / "wt"))
    summary.config.summary_cache_file().write_text(
        json.dumps({"version": 999, "sessions": {"x": {"summary": "stale"}}})
    )
    assert summary.load_cache() == {}
