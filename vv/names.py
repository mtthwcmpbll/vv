"""Random, memorable ``<adjective>-<animal>`` names for worktrees / tmux sessions."""

from __future__ import annotations

import itertools
import random
from collections.abc import Iterable

# Two curated pools that combine into the session name. Every entry must be
# safe as both a git branch name and a tmux session name (no '.', ':', '/' or
# spaces), and short enough that `<adjective>-<animal>` still types easily.
#
# Positive adjectives only: a session name is read dozens of times a day, so
# the pool leans encouraging rather than merely neutral.
ADJECTIVES: tuple[str, ...] = (
    "able", "adept", "agile", "amber", "ample", "apt", "ardent", "artful",
    "astute", "avid", "balmy", "blithe", "bold", "bonny", "brave", "brainy",
    "breezy", "bright", "brisk", "bubbly", "buoyant", "calm", "candid",
    "canny", "capable", "cheery", "chipper", "civic", "classy", "clear",
    "clever", "comfy", "cosmic", "cosy", "crafty", "crisp", "cunning",
    "curious", "daring", "dapper", "dazzling", "deft", "devoted", "dandy",
    "eager", "earnest", "easy", "elated", "electric", "elegant", "epic",
    "exact", "fabled", "fair", "faithful", "famous", "fancy", "fearless",
    "festive", "fiery", "fleet", "fluent", "fond", "frank",
    "free", "fresh", "friendly", "frisky", "gallant", "game", "generous",
    "gentle", "genuine", "giddy", "gifted", "glad", "gleaming", "glowing",
    "golden", "graceful", "grand", "grateful", "great", "hardy", "happy",
    "harmonic", "hearty", "helpful", "heroic", "honest", "hopeful", "humble",
    "jaunty", "jolly", "jovial", "joyful", "keen", "kind", "kindly", "lively",
    "logical", "loyal", "lucid", "lucky", "lush", "magic", "main", "merry",
    "mighty", "mindful", "modest", "neat", "nifty", "nimble", "noble", "novel",
    "peaceful", "peppy", "perky", "placid", "playful", "plucky", "polished",
    "poised", "prime", "prized", "proud", "prudent", "quick", "quiet", "radiant",
    "rapid", "ready", "regal", "resolute", "rich", "robust", "rosy", "royal",
    "rustic", "sage", "savvy", "scenic", "serene", "sharp", "shiny", "silky",
    "sincere", "skilled", "sleek", "smart", "smooth", "snappy", "snug", "solid",
    "sound", "spry", "stable", "starry", "steady", "stellar", "sterling",
    "stout", "sturdy", "suave", "sunny", "super", "supple", "swift", "tactful",
    "tidy", "timely", "tranquil", "trusty", "truthful", "upbeat", "urbane",
    "valiant", "vibrant", "vigilant", "vital", "vivid", "warm", "wise",
    "witty", "wondrous", "worthy", "zealous", "zesty", "zippy",
)

ANIMALS: tuple[str, ...] = (
    "adder", "alpaca", "antelope", "ape", "auk", "badger", "bat", "beagle",
    "bear", "beaver", "bee", "beetle", "bison", "bobcat", "bonobo", "boar",
    "bream", "buffalo", "bulldog", "bunting", "camel", "caribou", "cat",
    "cheetah", "chimp", "chinchilla", "chipmunk", "cobra", "cod", "colt",
    "condor", "coral", "corgi", "cougar", "coyote", "crab", "crane", "cricket",
    "crow", "cuckoo", "curlew", "deer", "dingo", "dodo", "dolphin", "donkey",
    "dormouse", "dove", "dragonfly", "drake", "duck", "eagle", "egret", "eel",
    "eland", "elk", "emu", "ermine", "falcon", "fawn", "ferret", "finch",
    "firefly", "fish", "flamingo", "fox", "gannet", "gazelle", "gecko",
    "gibbon", "giraffe", "gnu", "goat", "goose", "gopher", "grebe", "grouse",
    "guppy", "hamster", "hare", "hawk", "hedgehog", "heron", "hornet", "horse",
    "hound", "husky", "ibex", "ibis", "iguana", "impala", "jackal", "jaguar",
    "jay", "kestrel", "kingfisher", "kite", "kiwi", "koala", "krill", "lark",
    "lemming", "lemur", "leopard", "lion", "lizard", "llama", "lobster",
    "loon", "lynx", "macaw", "magpie", "mallard", "manatee", "mantis",
    "marlin", "marmot", "marten", "meerkat", "mink", "mole", "mongoose",
    "monkey", "moose", "moth", "mouse", "mule", "narwhal", "newt", "nightjar",
    "ocelot", "octopus", "okapi", "opossum", "orca", "oriole", "oryx",
    "osprey", "ostrich", "otter", "owl", "ox", "oyster", "panda", "pangolin",
    "panther", "parrot", "peacock", "pelican", "penguin", "perch", "pheasant",
    "pigeon", "pika", "pony", "porpoise", "prawn", "puffin", "puma", "quail",
    "quokka", "rabbit", "raccoon", "ram", "raven", "ray", "reindeer",
    "rhino", "robin", "rook", "salmon", "sandpiper", "seal", "serval",
    "shrew", "skink", "skua", "sloth", "snail", "snipe", "sparrow", "spider",
    "squid", "squirrel", "starling", "stingray", "stoat", "stork", "sturgeon",
    "swallow", "swan", "swift", "tapir", "teal", "tern", "thrush", "tiger",
    "toad", "tortoise", "toucan", "trout", "tuna", "turtle", "urchin", "viper",
    "vole", "vulture", "wallaby", "walrus", "warbler", "wasp", "weasel",
    "whale", "wolf", "wombat", "woodpecker", "wren", "yak", "zebra", "zebu",
)

#: How many random draws to try before falling back to enumerating the pool.
#: The product is tens of thousands of names, so a free one is nearly always
#: found on the first draw; enumerating is only for a pathologically full pool.
_DRAW_ATTEMPTS = 50


def all_names() -> tuple[str, ...]:
    """Return every ``<adjective>-<animal>`` combination."""
    return tuple(f"{a}-{n}" for a, n in itertools.product(ADJECTIVES, ANIMALS))


def random_name(taken: Iterable[str] = ()) -> str:
    """Return a random ``<adjective>-<animal>`` name not present in ``taken``.

    Falls back to suffixing a number if every combination is somehow taken.
    """
    taken_set = set(taken)

    for _ in range(_DRAW_ATTEMPTS):
        name = f"{random.choice(ADJECTIVES)}-{random.choice(ANIMALS)}"
        if name not in taken_set:
            return name

    available = [name for name in all_names() if name not in taken_set]
    if available:
        return random.choice(available)

    # Extremely unlikely, but stay deterministic-ish and collision-free.
    base = f"{random.choice(ADJECTIVES)}-{random.choice(ANIMALS)}"
    suffix = 2
    while f"{base}{suffix}" in taken_set:
        suffix += 1
    return f"{base}{suffix}"
