"""Tests for the random worktree-name picker."""

from __future__ import annotations

import re

import pytest

from vv import names

POOLS = {"ADJECTIVES": names.ADJECTIVES, "ANIMALS": names.ANIMALS}


@pytest.mark.parametrize("pool_name", sorted(POOLS))
def test_pool_words_are_valid_branch_and_session_names(pool_name):
    # Worktree names double as git branch and tmux session names.
    forbidden = set(" .:/")
    for word in POOLS[pool_name]:
        assert word, f"{pool_name} contains an empty entry"
        assert not (set(word) & forbidden), f"{word!r} has a forbidden character"
        # A '-' would make the two halves of a name ambiguous to read.
        assert "-" not in word, f"{word!r} contains a hyphen"


@pytest.mark.parametrize("pool_name", sorted(POOLS))
def test_pool_words_are_unique(pool_name):
    pool = POOLS[pool_name]
    assert len(pool) == len(set(pool))


def test_generated_names_are_adjective_animal():
    for _ in range(200):
        name = names.random_name()
        assert re.fullmatch(r"[a-z]+-[a-z]+", name), name
        adjective, animal = name.split("-")
        assert adjective in names.ADJECTIVES
        assert animal in names.ANIMALS


def test_all_names_is_the_full_product():
    assert len(names.all_names()) == len(names.ADJECTIVES) * len(names.ANIMALS)
    assert len(set(names.all_names())) == len(names.all_names())


def test_random_name_returns_a_name_when_none_taken():
    assert names.random_name() in set(names.all_names())


def test_random_name_returns_the_only_free_name():
    all_names = names.all_names()
    taken = set(all_names[:-1])  # everything except the last combination
    assert names.random_name(taken) == all_names[-1]


def test_random_name_never_returns_a_taken_name():
    taken = {"brave-otter", "swift-falcon", "clever-raven"}
    for _ in range(200):
        assert names.random_name(taken) not in taken


def test_random_name_accepts_any_iterable():
    first_five = names.all_names()[:5]
    name = names.random_name(iter(first_five))
    assert name in set(names.all_names())
    assert name not in first_five


def test_random_name_suffixes_when_every_name_is_taken():
    taken = set(names.all_names())
    name = names.random_name(taken)
    assert name not in taken
    assert any(name == f"{combo}2" for combo in taken)


def test_random_name_increments_suffix_past_collisions():
    combos = set(names.all_names())
    taken = combos | {f"{combo}2" for combo in combos}
    name = names.random_name(taken)
    assert name not in taken
    assert any(name == f"{combo}3" for combo in combos)
