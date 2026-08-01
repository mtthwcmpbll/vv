"""Tests for the session notes store (`vv --title` / `vv --label`)."""

from __future__ import annotations

import json

import pytest

from vv import notes


@pytest.fixture(autouse=True)
def store(monkeypatch, tmp_path):
    """Point the notes store at a throwaway worktrees dir."""
    monkeypatch.setenv("WORKTREES_DIR", str(tmp_path / "wt"))
    return tmp_path / "wt" / ".session-notes.json"


def _entries(store):
    return json.loads(store.read_text())["sessions"]


# --- labels -----------------------------------------------------------------

def test_parse_spec_add_remove_and_explicit_plus():
    assert notes.parse_spec("acme") == ("add", "acme")
    assert notes.parse_spec("  acme  ") == ("add", "acme")
    assert notes.parse_spec("+acme") == ("add", "acme")
    assert notes.parse_spec("-acme") == ("remove", "acme")
    assert notes.parse_spec("-Big Customer") == ("remove", "Big Customer")


def test_parse_spec_rejects_a_bare_sign():
    for bad in ("", "   ", "-", "+", "- "):
        with pytest.raises(notes.LabelError):
            notes.parse_spec(bad)


def test_apply_labels_adds_in_order_and_reports_changes():
    current, added, removed = notes.apply_labels("repo", "alpha", ["acme", "urgent"])
    assert current == ["acme", "urgent"]
    assert added == ["acme", "urgent"] and removed == []
    assert notes.for_session("repo", "alpha").labels == ["acme", "urgent"]


def test_apply_labels_persists_across_calls_and_appends():
    notes.apply_labels("repo", "alpha", ["acme"])
    current, added, _removed = notes.apply_labels("repo", "alpha", ["urgent"])
    assert current == ["acme", "urgent"] and added == ["urgent"]


def test_re_adding_an_existing_label_is_a_no_op():
    notes.apply_labels("repo", "alpha", ["acme"])
    current, added, removed = notes.apply_labels("repo", "alpha", ["ACME"])
    assert current == ["acme"]           # original casing kept, no duplicate
    assert added == [] and removed == []


def test_remove_is_case_insensitive_and_tolerates_a_missing_label():
    notes.apply_labels("repo", "alpha", ["Acme", "urgent"])
    current, _added, removed = notes.apply_labels("repo", "alpha", ["-ACME", "-nope"])
    assert current == ["urgent"]
    assert removed == ["Acme"]           # reports what was actually there


def test_add_and_remove_in_one_call_are_applied_in_order():
    notes.apply_labels("repo", "alpha", ["acme"])
    current, added, removed = notes.apply_labels("repo", "alpha", ["-acme", "beta"])
    assert current == ["beta"] and added == ["beta"] and removed == ["acme"]


def test_a_bad_spec_rejects_the_whole_call_without_writing():
    notes.apply_labels("repo", "alpha", ["acme"])
    with pytest.raises(notes.LabelError):
        notes.apply_labels("repo", "alpha", ["urgent", "-"])
    assert notes.for_session("repo", "alpha").labels == ["acme"]  # not half-applied


# --- titles -----------------------------------------------------------------

def test_set_title_stores_and_returns_it():
    assert notes.set_title("repo", "alpha", "Acme onboarding") == "Acme onboarding"
    assert notes.for_session("repo", "alpha").title == "Acme onboarding"


def test_set_title_collapses_whitespace_to_one_line():
    assert notes.set_title("repo", "alpha", "  Acme\n  onboarding  ") == "Acme onboarding"


def test_a_blank_title_clears_it():
    notes.set_title("repo", "alpha", "Acme")
    assert notes.set_title("repo", "alpha", "   ") is None
    assert notes.for_session("repo", "alpha").title is None


def test_title_and_labels_are_independent():
    notes.set_title("repo", "alpha", "Acme")
    notes.apply_labels("repo", "alpha", ["urgent"])
    note = notes.for_session("repo", "alpha")
    assert note.title == "Acme" and note.labels == ["urgent"]

    notes.set_title("repo", "alpha", "")                 # clearing one…
    assert notes.for_session("repo", "alpha").labels == ["urgent"]   # …keeps the other
    notes.set_title("repo", "alpha", "Acme")
    notes.apply_labels("repo", "alpha", ["-urgent"])
    assert notes.for_session("repo", "alpha").title == "Acme"


# --- the store --------------------------------------------------------------

def test_sessions_are_kept_apart_and_chats_are_just_another_repo():
    notes.apply_labels("repo", "alpha", ["acme"])
    notes.set_title("_chats", "spark", "Scratch space")
    assert notes.for_session("repo", "alpha") == notes.Note(labels=["acme"])
    assert notes.for_session("_chats", "spark") == notes.Note(title="Scratch space")
    assert set(notes.all_notes()) == {"repo/alpha", "_chats/spark"}


def test_a_session_with_nothing_left_drops_out_of_the_store(store):
    notes.set_title("repo", "alpha", "Acme")
    notes.apply_labels("repo", "alpha", ["acme"])
    notes.apply_labels("repo", "alpha", ["-acme"])
    notes.set_title("repo", "alpha", "")
    assert _entries(store) == {}
    assert not notes.for_session("repo", "alpha")


def test_forget_removes_one_session_only():
    notes.apply_labels("repo", "alpha", ["acme"])
    notes.apply_labels("repo", "beta", ["urgent"])
    notes.forget("repo", "alpha")
    assert list(notes.all_notes()) == ["repo/beta"]


def test_forget_repo_removes_every_session_of_that_repo():
    notes.apply_labels("repo", "alpha", ["acme"])
    notes.set_title("repo", "beta", "Second")
    notes.apply_labels("other", "gamma", ["keep"])
    notes.forget_repo("repo")
    assert list(notes.all_notes()) == ["other/gamma"]


def test_missing_store_reads_as_empty():
    assert notes.load() == {}
    assert not notes.for_session("repo", "alpha")


def test_corrupt_or_outdated_store_reads_as_empty(store):
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("{not json")
    assert notes.load() == {}
    store.write_text(json.dumps({
        "version": notes.STORE_VERSION + 1,
        "sessions": {"r/a": {"title": "x", "labels": []}},
    }))
    assert notes.load() == {}


def test_malformed_entries_are_filtered_out(store):
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({
        "version": notes.STORE_VERSION,
        "sessions": {
            "r/a": {"title": "ok", "labels": ["keep", 7, None]},
            "r/b": {"title": 42, "labels": "notalist"},   # nothing usable -> dropped
            "r/c": ["not", "a", "dict"],
        },
    }))
    assert notes.load() == {"r/a": notes.Note(title="ok", labels=["keep"])}
