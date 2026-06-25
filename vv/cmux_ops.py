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
import os
import re
import shutil
import subprocess
import time
from collections.abc import Sequence


class CmuxError(RuntimeError):
    """Raised when a cmux command fails (or cmux is missing)."""


# A freshly-opened SSH workspace's shell is "ready" once its prompt has been
# drawn. We detect that heuristically: the last non-blank line on screen ends in
# a conventional prompt sigil ($, #, %, or >), optionally followed by spaces.
_PROMPT_RE = re.compile(r"[$#%>]\s*$")


def _run(args: list[str], *, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["cmux", *args],
            check=check,
            text=True,
            # Silence the "X is now an alias for Y" deprecation notice so it
            # never contaminates captured output.
            env={**os.environ, "CMUX_QUIET": "1"},
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


def read_screen(workspace_id: str) -> str:
    """Return the visible screen text of ``workspace_id`` (best effort).

    Returns an empty string if the workspace can't be read yet (e.g. the SSH
    session is still connecting) so callers can poll without special-casing.
    """
    result = _run(
        ["read-screen", "--workspace", workspace_id, "--json"],
        capture=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    try:
        data = json.loads(result.stdout or "")
    except json.JSONDecodeError:
        return ""
    text = data.get("text") if isinstance(data, dict) else None
    return text if isinstance(text, str) else ""


def wait_until_ready(
    workspace_id: str,
    *,
    delay: float = 0.0,
    timeout: float = 20.0,
    interval: float = 0.4,
) -> bool:
    """Block until ``workspace_id``'s shell looks ready to accept a command.

    A freshly-opened ``cmux ssh`` workspace isn't immediately interactive: the
    SSH session is still connecting and the remote shell hasn't drawn its
    prompt. Typing into that window races the shell's startup — characters (in
    particular the submitting Enter) get swallowed, so the command is left
    sitting unrun. We avoid that by polling :func:`read_screen` until either a
    shell prompt appears (the positive signal) or the screen has gone quiet —
    non-empty and unchanged across two polls (a fallback for unrecognised
    prompts). Returns ``True`` once ready, ``False`` if ``timeout`` elapses
    first (the caller may still send, just without the guarantee).

    ``delay`` is an unconditional head-start sleep before any polling, for when
    the caller knows the login takes a moment (e.g. a slow host or a 2FA step):
    it lets the connection get past its noisy opening before we start reading
    the screen, and is *not* counted against ``timeout``.
    """
    if delay > 0:
        time.sleep(delay)
    deadline = time.monotonic() + timeout
    previous = ""
    while time.monotonic() < deadline:
        screen = read_screen(workspace_id)
        lines = [line for line in screen.splitlines() if line.strip()]
        if lines and _PROMPT_RE.search(lines[-1]):
            return True
        if screen and screen == previous:
            return True
        previous = screen
        time.sleep(interval)
    return False


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
