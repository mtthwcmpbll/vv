"""Whose turn is it? — the triage state of a vv session.

The session cards answer *what* a session is: its summary, its branch, its PR.
That is the wrong question when twenty of them are open. The question then is
**which one is waiting on me**, and nothing in the card layout answered it — the
running dot only ever meant "a tmux session exists", which is true of every
session you have not deleted.

This module computes the missing axis. Five states, ordered by how much they
want from you (:data:`ORDER`):

``needs_you``
    The agent stopped talking and it is your move. This is the lane the whole
    module exists to fill, so it is kept deliberately narrow — see below.
``stopped``
    The tmux session is alive but its pane is back at a shell: whatever vv
    launched has exited. Easy to miss otherwise, because such a session looks
    exactly like a healthy one from the outside.
``working``
    The agent produced output within :attr:`Windows.active`. Nothing to do.
``review``
    Pushed, with a PR open — CI and reviewers have it, not you.
``idle``
    Nothing is pending here. Where the pile is meant to end up, and what the
    stale sweep eats.

Three signals feed it, all of them already paid for elsewhere in vv:

* tmux's own ``session_activity`` and the active pane's foreground command, from
  the single whole-server call in :func:`tmux_ops.session_activity`;
* the session's last real conversation turn (:func:`summary.last_turn`), which
  reuses the transcript readers the summaries already depend on;
* the git/PR facts the cards compute anyway (:class:`Facts`).

No agent is ever run from here, and nothing is fetched over the network, so this
is safe to recompute on a timer.

**Why ``needs_you`` expires.** An agent that stopped an hour ago is waiting on
you. One that stopped last Tuesday is not — it is abandoned, and treating it as
urgent is exactly how the pile got indistinguishable in the first place. So a
session drops out of ``needs_you`` once it has been quiet for
:attr:`Windows.stale`, landing in ``review`` or ``idle`` where it can be swept.
The lane therefore holds today's work, not everything ever started.
"""

from __future__ import annotations

import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import summary, tmux_ops

#: The agent stopped and it is your move.
NEEDS_YOU = "needs_you"
#: The tmux session is alive but the agent that ran in it has exited.
STOPPED = "stopped"
#: The agent is producing output right now.
WORKING = "working"
#: Pushed with an open PR — waiting on CI or a reviewer.
REVIEW = "review"
#: Nothing pending.
IDLE = "idle"

#: Every state, most-wants-you-first. Doubles as the board's lane order and the
#: sort key for the session list, so both read as a queue rather than a pile.
ORDER: tuple[str, ...] = (NEEDS_YOU, STOPPED, WORKING, REVIEW, IDLE)

#: Human-readable lane titles.
LABELS: dict[str, str] = {
    NEEDS_YOU: "Needs you",
    STOPPED: "Stopped",
    WORKING: "Working",
    REVIEW: "In review",
    IDLE: "Idle",
}

#: Rank of each state within :data:`ORDER`, for sorting.
RANK: dict[str, int] = {name: index for index, name in enumerate(ORDER)}

#: Longest agent message kept as a state's ``detail``. The card truncates
#: further to fit; this only stops a wall of text reaching it.
MAX_DETAIL = 160


@dataclass(frozen=True)
class Windows:
    """The two quiet-periods that separate the states, both in seconds.

    ``active`` is how long a session may go without output and still count as
    ``working`` — long enough to cover an agent thinking or running a slow tool,
    short enough that a finished one surfaces promptly. ``stale`` is how long
    ``needs_you`` lasts before the session is written off as abandoned rather
    than waiting (see the module docstring).
    """

    active: float = 60.0
    stale: float = 86_400.0  # 24h — "I left it overnight" still counts as waiting


#: Windows used when the caller passes none (and the config sets none).
DEFAULT_WINDOWS = Windows()


@dataclass(frozen=True)
class Facts:
    """What a session's own files say about it — everything tmux cannot know.

    ``dirty`` and ``pr`` are the values the cards already compute
    (:func:`cli._worktree_dirty` and the :class:`pr.Snapshot` cache), passed in
    rather than recomputed so classifying a board full of sessions costs no extra
    git or ``gh`` calls.
    """

    name: str
    path: Path
    dirty: bool = False
    pr: dict | None = None


@dataclass(frozen=True)
class SessionState:
    """A session's triage state, plus the short "why" behind it.

    ``detail`` is the agent's own last words for a ``needs_you`` session — the
    single most useful thing to put on its card, because "Should I also update
    the tests?" tells you what to do and a summary of the work does not.
    """

    state: str
    detail: str | None = None

    @property
    def label(self) -> str:
        """The human-readable lane title for this state."""
        return LABELS.get(self.state, self.state)

    @property
    def rank(self) -> int:
        """Position in :data:`ORDER`; unknown states sort last."""
        return RANK.get(self.state, len(ORDER))


def classify(
    *,
    activity: "tmux_ops.Activity | None" = None,
    last_turn: tuple[str, str] | None = None,
    dirty: bool = False,
    pr: dict | None = None,
    windows: Windows | None = None,
    now: float | None = None,
) -> SessionState:
    """Work out a single session's state from its three signals.

    ``activity`` is ``None`` when no tmux session is running for it. Pure and
    fully injectable — every input is an argument, so the whole state machine is
    testable without a tmux server, a transcript, or a repo.
    """
    windows = windows or DEFAULT_WINDOWS
    now = time.time() if now is None else now

    # No tmux session at all: nothing is running, so nothing is waiting on you.
    # The work decides — an open PR means the world has it, otherwise it is
    # parked. (Uncommitted work here is not "urgent": it is already flagged by
    # the card's dirty marker, and the sweep refuses to delete it.)
    if activity is None:
        return SessionState(REVIEW if _pr_open(pr) else IDLE)

    quiet_for = max(0.0, now - activity.last_activity)

    # The pane is back at a shell, so whatever vv launched is gone. Only trust
    # that once the session has also fallen quiet: an agent that shells out for a
    # tool call can briefly put a shell in the foreground while output still flows.
    if activity.at_shell and quiet_for >= windows.active:
        return SessionState(STOPPED)

    if quiet_for < windows.active:
        return SessionState(WORKING)

    # Alive but quiet, and not yet abandoned: whoever spoke last decides. The
    # agent -> it is sitting at its prompt waiting on you; you -> it is still
    # chewing on your request and simply slower than the active window.
    role, text = last_turn or (None, "")
    if quiet_for < windows.stale:
        if role == "assistant":
            return SessionState(NEEDS_YOU, _detail(text))
        if role == "user":
            return SessionState(WORKING)

    # Quiet past the stale window (or no transcript to read at all): it is not
    # waiting on you any more, it is just sitting there.
    return SessionState(REVIEW if _pr_open(pr) else IDLE)


def classify_all(
    sessions: dict[object, Facts],
    *,
    windows: Windows | None = None,
    now: float | None = None,
) -> dict[object, SessionState]:
    """Classify many sessions at once, keyed exactly like ``sessions``.

    tmux is asked once for the whole server, and the per-session transcript reads
    are fanned out over a small pool (they are memoized on the transcript's mtime,
    so only sessions that have actually moved cost anything on a re-run). Missing
    tmux degrades to "no session is running" rather than raising — the states are
    UI decoration, and a board that dies because tmux is not installed is worse
    than one that shows everything as idle.
    """
    try:
        activities = tmux_ops.session_activity()
    except tmux_ops.TmuxError:
        activities = {}
    if not sessions:
        return {}

    keys = list(sessions)
    workers = min(len(keys), _TURN_WORKERS)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        turns = list(pool.map(lambda key: _safe_last_turn(sessions[key].path), keys))

    return {
        key: classify(
            activity=activities.get(sessions[key].name),
            last_turn=turn,
            dirty=sessions[key].dirty,
            pr=sessions[key].pr,
            windows=windows,
            now=now,
        )
        for key, turn in zip(keys, turns)
    }


#: Cap on concurrent transcript reads in :func:`classify_all`.
_TURN_WORKERS = 8


def _safe_last_turn(path: Path) -> tuple[str, str] | None:
    """:func:`summary.last_turn`, with any failure meaning "no transcript"."""
    try:
        return summary.last_turn(path)
    except Exception:  # noqa: BLE001 — a state is never worth an exception
        return None


def _pr_open(pr: dict | None) -> bool:
    """True for a PR still in flight; merged and closed ones are finished work."""
    return bool(pr) and pr.get("state") in ("open", "draft")


def _detail(text: str) -> str | None:
    """Collapse an agent's last message to one short line, or ``None`` if empty."""
    collapsed = " ".join((text or "").split())
    if not collapsed:
        return None
    return textwrap.shorten(collapsed, width=MAX_DETAIL, placeholder="…")
