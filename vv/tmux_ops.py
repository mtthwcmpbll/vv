"""Thin wrappers around the ``tmux`` CLI."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class TmuxError(RuntimeError):
    """Raised when a tmux command fails."""


def _run(args: list[str], *, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["tmux", *args],
            check=check,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except FileNotFoundError as exc:
        raise TmuxError("tmux is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or f"exit code {exc.returncode}"
        raise TmuxError(f"`tmux {' '.join(args)}` failed: {detail}") from exc


#: tmux session option stamped on every session vv creates, so the menu can
#: tell vv sessions apart from any other tmux sessions on the machine.
VV_TAG = "@vv"


def list_sessions(*, vv_only: bool = False) -> list[str]:
    """Return the names of running tmux sessions.

    With ``vv_only`` set, restrict the result to sessions created by vv
    (those carrying the ``@vv`` session option); otherwise return every
    session, which is what collision-avoidance needs.
    """
    result = _run(
        ["list-sessions", "-F", f"#{{session_name}}\t#{{{VV_TAG}}}"],
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        # No server running yet -> no sessions.
        return []
    sessions: list[str] = []
    for line in (result.stdout or "").splitlines():
        name, _, tag = line.partition("\t")
        name = name.strip()
        if not name:
            continue
        if vv_only and tag.strip() != "1":
            continue
        sessions.append(name)
    return sessions


def session_exists(name: str) -> bool:
    """Return True if a session called ``name`` already exists."""
    result = _run(["has-session", "-t", f"={name}"], capture=True, check=False)
    return result.returncode == 0


def kill_session(name: str) -> None:
    """Kill session ``name``; no error if it is already gone."""
    _run(["kill-session", "-t", f"={name}"], capture=True, check=False)


def create_session(name: str, cwd: Path) -> None:
    """Create a detached session ``name`` rooted at ``cwd``, tagged as vv's."""
    _run(["new-session", "-d", "-s", name, "-c", str(cwd)])
    _run(["set-option", "-t", f"={name}:", VV_TAG, "1"])


def send_command(name: str, command: str) -> None:
    """Type ``command`` followed by Enter into session ``name``.

    The trailing colon makes this an exact-match session target that resolves
    to the session's active pane (``send-keys`` targets a pane, not a session).
    """
    _run(["send-keys", "-t", f"={name}:", command, "Enter"])


def attach(name: str) -> None:
    """Attach to ``name``, or switch to it if already inside tmux."""
    if os.environ.get("TMUX"):
        _run(["switch-client", "-t", f"={name}"])
    else:
        # Replace the current process so the user lands directly in tmux.
        os.execvp("tmux", ["tmux", "attach-session", "-t", f"={name}"])
