"""User-set notes on vv sessions: a manual title and a set of labels.

These are the manual levers for telling many sessions apart. The **title** is a
one-line description you write yourself (shown above the generated summary on a
session card); **labels** are free-text tags — a customer name, a ticket,
"urgent". Both are *user data*, not derived: unlike :mod:`vv.summary` /
:mod:`vv.pr` nothing regenerates them, so the store is only ever rewritten by an
explicit ``vv --title`` / ``vv --label`` (or when a session is deleted and its
entry is forgotten).

The store is a single JSON file (:func:`vv.config.notes_file`) keyed by the same
``"<repo>/<name>"`` session id the caches use, so chat sessions (under the
``_chats`` sentinel repo) work exactly like worktree-backed ones.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import config

#: Bump when the on-disk shape changes, to discard an outdated store.
STORE_VERSION = 1


class LabelError(ValueError):
    """Raised for a label spec the user typed that vv cannot act on."""


@dataclass(frozen=True)
class Note:
    """One session's manual title and labels (both optional)."""

    title: str | None = None
    labels: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        """False when there is nothing worth storing (or rendering)."""
        return bool(self.title or self.labels)


@dataclass(frozen=True)
class Pending:
    """Notes to stamp on a session vv is about to create (from the CLI flags)."""

    title: str | None = None
    label_specs: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.title is not None or bool(self.label_specs)


def session_id(repo: str, name: str) -> str:
    """The store key for a session: ``"<repo>/<name>"``."""
    return f"{repo}/{name}"


def load() -> dict[str, Note]:
    """Load the whole store as ``{session_id: Note}``.

    Returns ``{}`` for a missing, unreadable, or version-mismatched file — a
    broken store costs the user their notes, but must never break the menu.
    Malformed entries are skipped rather than trusted.
    """
    try:
        with config.notes_file().open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
        return {}
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        return {}

    notes: dict[str, Note] = {}
    for key, entry in sessions.items():
        if not isinstance(entry, dict):
            continue
        title = entry.get("title")
        raw_labels = entry.get("labels")
        note = Note(
            title=title if isinstance(title, str) and title else None,
            labels=[item for item in raw_labels if isinstance(item, str)]
            if isinstance(raw_labels, list)
            else [],
        )
        if note:
            notes[key] = note
    return notes


def save(notes: dict[str, Note]) -> None:
    """Write the store atomically, dropping sessions with nothing left on them."""
    path = config.notes_file()
    tmp = path.with_name(path.name + ".tmp")
    payload = {
        key: {"title": note.title, "labels": note.labels}
        for key, note in notes.items()
        if note
    }
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump({"version": STORE_VERSION, "sessions": payload}, handle)
        tmp.replace(path)
    except OSError:
        pass


def for_session(repo: str, name: str) -> Note:
    """One session's note (an empty :class:`Note` when it has none)."""
    return load().get(session_id(repo, name), Note())


def all_notes() -> dict[str, Note]:
    """The whole store, for callers rendering many sessions at once."""
    return load()


def clean_title(title: str) -> str | None:
    """Normalize a title to a single line, or ``None`` when it clears the title.

    Runs of whitespace collapse to single spaces so a pasted multi-line string
    can't wreck the card layout; an empty (or all-whitespace) title means
    "remove the title I set", which is how ``vv --title ""`` clears it.
    """
    collapsed = " ".join(title.split())
    return collapsed or None


def set_title(repo: str, name: str, title: str) -> str | None:
    """Set (or, given a blank ``title``, clear) a session's title.

    Returns the stored title, or ``None`` if it was cleared.
    """
    store = load()
    key = session_id(repo, name)
    cleaned = clean_title(title)
    existing = store.get(key, Note())
    store[key] = Note(title=cleaned, labels=existing.labels)
    save(store)
    return cleaned


def parse_spec(spec: str) -> tuple[str, str]:
    """Split a ``--label`` argument into an ``("add" | "remove", label)`` pair.

    A leading ``-`` removes the label, a leading ``+`` adds it explicitly, and
    anything else is an add. Surrounding whitespace is stripped; a spec with no
    label left after the sign is an error rather than a silent no-op.
    """
    raw = spec.strip()
    action = "add"
    if raw.startswith("-"):
        action, raw = "remove", raw[1:]
    elif raw.startswith("+"):
        raw = raw[1:]
    label = raw.strip()
    if not label:
        raise LabelError(f"'{spec}' is not a label (nothing after the sign)")
    return action, label


def apply_labels(
    repo: str, name: str, specs: list[str]
) -> tuple[list[str], list[str], list[str]]:
    """Apply ``specs`` to a session's labels; return ``(labels, added, removed)``.

    ``labels`` is the resulting list in order (existing first, newly added
    appended), ``added`` / ``removed`` only what actually changed — re-adding a
    label a session already carries, or removing one it doesn't, is a no-op
    rather than an error. Matching is case-insensitive (so "Acme" and "acme"
    are the same label) while the casing the user typed is preserved. Every spec
    is parsed up front, so a typo in the last one can't half-apply the rest.
    """
    parsed = [parse_spec(spec) for spec in specs]

    store = load()
    key = session_id(repo, name)
    existing = store.get(key, Note())
    current = list(existing.labels)
    added: list[str] = []
    removed: list[str] = []

    for action, label in parsed:
        folded = label.casefold()
        matches = [item for item in current if item.casefold() == folded]
        if action == "add":
            if matches:
                continue
            current.append(label)
            added.append(label)
        else:
            if not matches:
                continue
            current = [item for item in current if item.casefold() != folded]
            removed.extend(matches)

    store[key] = Note(title=existing.title, labels=current)
    save(store)
    return current, added, removed


def forget(repo: str, name: str) -> None:
    """Drop a deleted session's note from the store (best-effort)."""
    store = load()
    if store.pop(session_id(repo, name), None) is not None:
        save(store)


def forget_repo(repo: str) -> None:
    """Drop the notes of every session belonging to a deleted repo."""
    store = load()
    prefix = f"{repo}/"
    remaining = {key: note for key, note in store.items() if not key.startswith(prefix)}
    if len(remaining) != len(store):
        save(remaining)
