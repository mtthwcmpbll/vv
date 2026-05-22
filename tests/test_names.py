"""Tests for the random worktree-name picker."""

from __future__ import annotations

from vv import names


def test_words_are_valid_branch_and_session_names():
    # Worktree names double as git branch and tmux session names.
    forbidden = set(" .:/")
    for word in names.WORDS:
        assert word, "WORDS contains an empty entry"
        assert not (set(word) & forbidden), f"{word!r} has a forbidden character"


def test_words_are_unique():
    assert len(names.WORDS) == len(set(names.WORDS))


def test_random_name_returns_a_word_when_none_taken():
    assert names.random_name() in names.WORDS


def test_random_name_returns_the_only_free_word():
    taken = set(names.WORDS[:-1])  # everything except the last word
    assert names.random_name(taken) == names.WORDS[-1]


def test_random_name_never_returns_a_taken_name():
    taken = {"falcon", "otter", "raven"}
    for _ in range(200):
        assert names.random_name(taken) not in taken


def test_random_name_accepts_any_iterable():
    name = names.random_name(iter(names.WORDS[:5]))
    assert name in names.WORDS
    assert name not in names.WORDS[:5]


def test_random_name_suffixes_when_every_word_is_taken():
    taken = set(names.WORDS)
    name = names.random_name(taken)
    assert name not in taken
    assert any(name == f"{word}2" for word in names.WORDS)


def test_random_name_increments_suffix_past_collisions():
    taken = set(names.WORDS) | {f"{word}2" for word in names.WORDS}
    name = names.random_name(taken)
    assert name not in taken
    assert any(name == f"{word}3" for word in names.WORDS)
