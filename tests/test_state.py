"""Tests for the triage state machine — whose turn is it on each session.

:func:`state.classify` is pure and fully injectable, so all of this runs without
a tmux server, a transcript, or a repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vv import state, tmux_ops


NOW = 1_000_000.0


def _activity(quiet_for: float, command: str = "claude", dead: bool = False):
    """A live session whose last output was ``quiet_for`` seconds ago."""
    return tmux_ops.Activity(
        last_activity=NOW - quiet_for, attached=False, command=command, dead=dead
    )


def _classify(**over):
    kwargs = {"now": NOW}
    kwargs.update(over)
    return state.classify(**kwargs)


# --- no tmux session: the work decides --------------------------------------

def test_dormant_session_is_idle():
    # Nothing is running, so nothing can be waiting on you.
    assert _classify(activity=None).state == state.IDLE


def test_dormant_session_with_open_pr_is_in_review():
    assert _classify(activity=None, pr={"state": "open"}).state == state.REVIEW
    assert _classify(activity=None, pr={"state": "draft"}).state == state.REVIEW


def test_finished_pr_is_not_review():
    # Merged and closed are finished work, not something the world still holds.
    assert _classify(activity=None, pr={"state": "merged"}).state == state.IDLE
    assert _classify(activity=None, pr={"state": "closed"}).state == state.IDLE


def test_dormant_session_with_uncommitted_work_is_still_idle():
    # Dirty is flagged on the card and blocks the sweep; it is not "urgent", or
    # every parked session with a stray edit would crowd the needs-you lane.
    assert _classify(activity=None, dirty=True).state == state.IDLE


# --- live sessions ----------------------------------------------------------

def test_recent_output_is_working_whoever_spoke_last():
    for turn in (("assistant", "done"), ("user", "go"), None):
        assert _classify(activity=_activity(5), last_turn=turn).state == state.WORKING


def test_quiet_with_agent_speaking_last_needs_you():
    result = _classify(
        activity=_activity(300), last_turn=("assistant", "Should I update the tests?")
    )
    assert result.state == state.NEEDS_YOU
    assert result.detail == "Should I update the tests?"   # the card's most useful line


def test_quiet_with_user_speaking_last_is_still_working():
    # The agent has not answered yet — it is chewing, just slower than the window.
    assert _classify(
        activity=_activity(300), last_turn=("user", "refactor this")
    ).state == state.WORKING


def test_quiet_without_a_transcript_falls_back_to_the_work():
    assert _classify(activity=_activity(300)).state == state.IDLE
    assert _classify(activity=_activity(300), pr={"state": "open"}).state == state.REVIEW


def test_needs_you_expires_into_idle():
    """An agent that stopped last week is abandoned, not waiting."""
    turn = ("assistant", "Anything else?")
    fresh = _classify(activity=_activity(300), last_turn=turn)
    ancient = _classify(activity=_activity(5 * 86400), last_turn=turn)
    assert fresh.state == state.NEEDS_YOU
    assert ancient.state == state.IDLE          # the pile drains out of the lane
    assert ancient.detail is None


def test_expired_needs_you_with_open_pr_goes_to_review():
    assert _classify(
        activity=_activity(5 * 86400),
        last_turn=("assistant", "Opened the PR."),
        pr={"state": "open"},
    ).state == state.REVIEW


# --- the agent exited -------------------------------------------------------

@pytest.mark.parametrize("shell", sorted(tmux_ops.SHELL_COMMANDS))
def test_pane_back_at_a_shell_is_stopped(shell):
    assert _classify(activity=_activity(300, command=shell)).state == state.STOPPED


def test_dead_pane_is_stopped():
    assert _classify(
        activity=_activity(300, command="claude", dead=True)
    ).state == state.STOPPED


def test_shell_in_the_foreground_while_output_flows_is_not_stopped():
    """An agent shelling out for a tool call must not read as a crash."""
    assert _classify(activity=_activity(2, command="bash")).state == state.WORKING


# --- windows are configurable ----------------------------------------------

def test_windows_are_honored():
    windows = state.Windows(active=5.0, stale=100.0)
    turn = ("assistant", "well?")
    assert _classify(activity=_activity(2), windows=windows).state == state.WORKING
    assert _classify(
        activity=_activity(50), last_turn=turn, windows=windows
    ).state == state.NEEDS_YOU
    assert _classify(
        activity=_activity(200), last_turn=turn, windows=windows
    ).state == state.IDLE


# --- detail normalization ---------------------------------------------------

def test_detail_collapses_whitespace_and_shortens():
    detail = _classify(
        activity=_activity(300), last_turn=("assistant", "line one\n\n  line   two")
    ).detail
    assert detail == "line one line two"

    long = _classify(
        activity=_activity(300), last_turn=("assistant", "word " * 200)
    ).detail
    assert len(long) <= state.MAX_DETAIL


def test_detail_none_for_an_empty_message():
    assert _classify(activity=_activity(300), last_turn=("assistant", "   ")).detail is None


# --- ordering ---------------------------------------------------------------

def test_order_is_most_wants_you_first():
    assert state.ORDER[0] == state.NEEDS_YOU
    assert state.ORDER[-1] == state.IDLE
    assert set(state.ORDER) == set(state.LABELS)
    assert state.SessionState(state.NEEDS_YOU).rank < state.SessionState(state.IDLE).rank


def test_unknown_state_sorts_last_and_labels_itself():
    unknown = state.SessionState("mystery")
    assert unknown.rank == len(state.ORDER)
    assert unknown.label == "mystery"


# --- classify_all -----------------------------------------------------------

def test_classify_all_asks_tmux_once(monkeypatch, tmp_path):
    calls = []

    def fake_activity(**_kwargs):
        calls.append(1)
        return {"alive": _activity(2)}

    monkeypatch.setattr(tmux_ops, "session_activity", fake_activity)
    monkeypatch.setattr(state, "_safe_last_turn", lambda _path: None)
    sessions = {
        ("r", "alive"): state.Facts(name="alive", path=tmp_path),
        ("r", "gone"): state.Facts(name="gone", path=tmp_path),
    }
    result = state.classify_all(sessions, now=NOW)
    assert len(calls) == 1                                  # one call for the server
    assert result[("r", "alive")].state == state.WORKING
    assert result[("r", "gone")].state == state.IDLE        # no tmux session


def test_classify_all_survives_missing_tmux(monkeypatch, tmp_path):
    def boom(**_kwargs):
        raise tmux_ops.TmuxError("tmux is not installed or not on PATH")

    monkeypatch.setattr(tmux_ops, "session_activity", boom)
    monkeypatch.setattr(state, "_safe_last_turn", lambda _path: None)
    sessions = {("r", "a"): state.Facts(name="a", path=tmp_path)}
    assert state.classify_all(sessions, now=NOW)[("r", "a")].state == state.IDLE


def test_classify_all_empty():
    assert state.classify_all({}) == {}


def test_safe_last_turn_swallows_failures(monkeypatch):
    monkeypatch.setattr(
        state.summary, "last_turn", lambda _p: (_ for _ in ()).throw(RuntimeError("x"))
    )
    assert state._safe_last_turn(Path("/nope")) is None
