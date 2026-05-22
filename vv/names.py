"""Random, memorable, single-word names for worktrees / tmux sessions."""

from __future__ import annotations

import random
from collections.abc import Iterable

# A curated list of short, memorable, easy-to-type single words. Anything here
# is safe to use as both a git branch name and a tmux session name.
WORDS: tuple[str, ...] = (
    "falcon", "otter", "badger", "lynx", "heron", "marten", "raven", "ibex",
    "puffin", "bison", "gecko", "panda", "tapir", "koala", "lemur", "moose",
    "narwhal", "ocelot", "quokka", "stork", "walrus", "wombat", "yak", "zebra",
    "comet", "nebula", "quasar", "pulsar", "meteor", "aurora", "cosmos",
    "ember", "cinder", "flint", "spark", "blaze", "glow", "prism",
    "willow", "cedar", "maple", "birch", "alder", "aspen", "hazel", "juniper",
    "basil", "clover", "fern", "ivy", "moss", "thyme", "sage", "sorrel",
    "river", "delta", "fjord", "lagoon", "harbor", "summit", "ridge", "canyon",
    "mesa", "tundra", "prairie", "glacier", "geyser", "reef", "atoll",
    "amber", "onyx", "jasper", "opal", "topaz", "garnet", "quartz", "slate",
    "copper", "cobalt", "indigo", "crimson", "violet", "scarlet", "saffron",
    "anchor", "beacon", "compass", "lantern", "rudder", "sextant", "mast",
    "pixel", "vector", "cipher", "kernel", "lambda", "matrix", "raster",
    "banjo", "cello", "fiddle", "harp", "lute", "oboe", "tabor", "viola",
    "mango", "guava", "lychee", "papaya", "quince", "kiwi", "fig", "plum",
    "arrow", "kite", "feather", "pebble", "acorn", "thistle", "bramble",
    "nimbus", "cirrus", "zephyr", "monsoon", "breeze", "tempest", "gale",
)


def random_name(taken: Iterable[str] = ()) -> str:
    """Return a random word not present in ``taken``.

    Falls back to suffixing a number if every word is somehow taken.
    """
    taken_set = set(taken)
    available = [w for w in WORDS if w not in taken_set]
    if available:
        return random.choice(available)

    # Extremely unlikely, but stay deterministic-ish and collision-free.
    base = random.choice(WORDS)
    suffix = 2
    while f"{base}{suffix}" in taken_set:
        suffix += 1
    return f"{base}{suffix}"
