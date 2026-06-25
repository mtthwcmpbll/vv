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
from collections.abc import Sequence


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


def new_ssh_workspace(
    target: str,
    *,
    name: str | None = None,
    port: int | None = None,
    identity: str | None = None,
    ssh_options: Sequence[str] = (),
) -> str | None:
    """Open a native cmux SSH workspace for ``target`` and return its id.

    Unlike a local pane that merely runs ``ssh``, ``cmux ssh`` makes the tab a
    first-class remote connection: it installs cmuxd-remote on the server and
    wires up agent notifications and session reconnect. That bootstrap only
    happens when **no** trailing command is passed (a remote command via ``--``
    turns cmux ssh into a plain ``ssh host cmd``), so we pass none and let the
    caller drive the session afterwards with :func:`send_text`. The workspace id
    is read back from ``--json`` so that follow-up can target this workspace
    precisely; ``None`` if the payload can't be parsed.

    ``ssh_options`` are forwarded one-per ``--ssh-option`` (cmux's ``-o
    Key=Value`` passthrough); ``port`` / ``identity`` map to ``--port`` /
    ``--identity``. cmux ssh also reads ``~/.ssh/config``, so host aliases and
    their identities work without configuring anything here.
    """
    args = ["ssh", target, "--json"]
    if name:
        args += ["--name", name]
    if port is not None:
        args += ["--port", str(port)]
    if identity:
        args += ["--identity", identity]
    for option in ssh_options:
        args += ["--ssh-option", option]
    result = _run(args, capture=True)
    return _parse_workspace_id(result.stdout)


def send_text(workspace_id: str, text: str) -> None:
    """Type ``text`` into ``workspace_id``'s focused surface.

    Used to run a command in a freshly-opened SSH workspace. cmux interprets the
    escape sequences ``\\n`` / ``\\r`` / ``\\t`` in the text, so a trailing
    literal ``\\n`` submits the line (it becomes a carriage return). ``text`` is
    passed as a single token after ``--`` so its spaces and quotes reach the
    remote shell verbatim — exactly as if the user had typed the command.
    """
    _run(["send", "--workspace", workspace_id, "--", text])


def _parse_workspace_id(stdout: str | None) -> str | None:
    """Pull the workspace's id/ref out of a ``cmux … --json`` payload."""
    try:
        data = json.loads(stdout or "")
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        for key in ("workspace_id", "workspace_ref", "id", "uuid", "ref", "workspace"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None
