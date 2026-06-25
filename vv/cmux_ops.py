"""Thin wrappers around the ``cmux`` CLI.

`cmux <https://cmux.com>`_ is a scriptable macOS terminal whose vertical-tab
"workspaces" can be driven from the command line. vv uses it in remote-launcher
mode: each remote session is surfaced as a local cmux workspace that SSHes into
the server (see :mod:`vv.remote`).

As with :mod:`vv.git_ops` / :mod:`vv.tmux_ops`, every call shells out to the
real CLI and failures surface as :class:`CmuxError`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class CmuxError(RuntimeError):
    """Raised when a cmux command fails (or cmux is missing)."""


def _run(args: list[str], *, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["cmux", *args],
            check=check,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except FileNotFoundError as exc:
        raise CmuxError("cmux is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip() or f"exit code {exc.returncode}"
        raise CmuxError(f"`cmux {' '.join(args)}` failed: {detail}") from exc


def is_available() -> bool:
    """Return True if the ``cmux`` CLI is on ``PATH``."""
    return shutil.which("cmux") is not None


def ensure_available() -> None:
    """Raise :class:`CmuxError` (with an install hint) if cmux is missing."""
    if not is_available():
        raise CmuxError(
            "cmux is not installed or not on PATH — remote mode needs it to "
            "open workspaces (see https://cmux.com)"
        )


def list_workspace_titles() -> list[str]:
    """Return the titles of existing cmux workspaces (best effort).

    Used only for local name-collision avoidance, so any failure (no cmux app
    running, unexpected JSON) degrades to an empty list rather than aborting.
    """
    result = _run(["list-workspaces", "--json"], capture=True, check=False)
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    # The CLI may return a bare list or wrap it under a "workspaces" key.
    items = data.get("workspaces", data) if isinstance(data, dict) else data
    titles: list[str] = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            title = item.get("title") or item.get("name")
            if isinstance(title, str) and title.strip():
                titles.append(title.strip())
    return titles


def new_workspace(cwd: Path, command: str, *, title: str | None = None) -> None:
    """Create a cmux workspace rooted at ``cwd`` running ``command``.

    When ``title`` is given the new workspace is renamed to it, so a remote
    session and its local tab share a name. The new workspace's id is read back
    from ``new-workspace --json`` to target the rename precisely; if that can't
    be parsed we fall back to a bare ``rename-workspace`` (which renames the
    just-created/current workspace).
    """
    result = _run(
        ["new-workspace", "--cwd", str(cwd), "--command", command, "--json"],
        capture=True,
        check=False,
    )
    # ``--json`` may be unsupported on older builds; retry plainly so creation
    # still happens. Without it we just can't read back the id.
    if result.returncode != 0:
        _run(["new-workspace", "--cwd", str(cwd), "--command", command])
        workspace_id = None
    else:
        workspace_id = _parse_workspace_id(result.stdout)

    if title:
        if workspace_id:
            _run(["rename-workspace", "--workspace", workspace_id, title])
        else:
            _run(["rename-workspace", title])


def _parse_workspace_id(stdout: str | None) -> str | None:
    """Pull the new workspace's id/ref out of ``new-workspace --json`` output."""
    try:
        data = json.loads(stdout or "")
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        for key in ("id", "uuid", "ref", "workspace"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None
