"""Thin wrappers around the ``tmux`` CLI."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote


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
    _setup_cwd_forwarding(name, cwd)


def _setup_cwd_forwarding(name: str, cwd: Path) -> None:
    """Re-report ``cwd`` to the outer terminal every time a client attaches.

    tmux consumes OSC 7 from its panes instead of relaying it, so terminals like
    cmux/Ghostty (which derive a tab's directory — and thus its git branch / PR /
    port info — from OSC 7) would freeze on whatever directory was reported at
    handover (see :func:`_report_cwd`). That one-shot report is also lost on any
    later re-attach (cmux/SSH reconnect, detach-and-reselect), where vv isn't in
    the loop to re-send it. So we turn on passthrough and install a
    ``client-attached`` hook that re-emits the worktree's OSC 7, wrapped for
    passthrough, on every attach.

    We bake in the worktree path rather than ``#{pane_current_path}``: it's the
    directory whose branch/PR cmux should show, it's immune to the transient cwd
    a shell reports mid-startup (an attach landing during rc-file sourcing would
    otherwise forward the wrong dir), and it needs no live format expansion.

    Best-effort: an older tmux without ``allow-passthrough`` (pre-3.3) or a vv we
    can't locate on PATH simply means no live cwd updates, so every call here is
    non-fatal (``check=False``) and we bail if vv can't be found.
    """
    self_cmd = _self_command()
    if self_cmd is None:
        return
    target = f"={name}:"
    _run(["set-option", "-t", target, "allow-passthrough", "on"], capture=True, check=False)
    # Double-quote the vv path and worktree (for /bin/sh) so spaces survive, and
    # keep the whole run-shell argument free of single quotes so tmux's own
    # single-quoting around it holds. #{pane_tty} is expanded by run-shell at fire
    # time to the attaching client's pane.
    inner = f'"{self_cmd}" --emit-cwd "{cwd}" > #{{pane_tty}}'
    hook = f"run-shell '{inner}'"
    _run(["set-hook", "-t", target, "client-attached", hook], capture=True, check=False)


def _self_command() -> str | None:
    """Absolute path to the running vv executable, for the cwd hook.

    Resolved through the current process's PATH (which is the user's interactive
    PATH, unlike the barer environment a tmux hook's shell may inherit) so the
    hook can invoke vv by absolute path. Returns None if vv can't be located.
    """
    exe = shutil.which(os.path.basename(sys.argv[0])) or sys.argv[0]
    exe = os.path.abspath(exe)
    return exe if os.path.exists(exe) else None


def send_command(name: str, command: str) -> None:
    """Type ``command`` followed by Enter into session ``name``.

    The trailing colon makes this an exact-match session target that resolves
    to the session's active pane (``send-keys`` targets a pane, not a session).
    """
    _run(["send-keys", "-t", f"={name}:", command, "Enter"])


def attach(name: str, cwd: Path | None = None) -> None:
    """Attach to ``name``, or switch to it if already inside tmux.

    When ``cwd`` is given and we are about to hand the terminal off (not already
    nested inside tmux), report it to the enclosing terminal via OSC 7 first so
    the terminal's tab reflects the worktree — see :func:`_report_cwd`.
    """
    if os.environ.get("TMUX"):
        _run(["switch-client", "-t", f"={name}"])
    else:
        if cwd is not None:
            _report_cwd(cwd)
        # Replace the current process so the user lands directly in tmux.
        os.execvp("tmux", ["tmux", "attach-session", "-t", f"={name}"])


def _report_cwd(cwd: Path) -> None:
    """Emit an OSC 7 sequence so the enclosing terminal shows ``cwd``.

    Terminals like cmux/Ghostty, iTerm2, WezTerm and kitty learn a tab's
    directory from the OSC 7 sequences a shell emits at each prompt. Once vv
    hands the terminal to ``tmux attach`` no outer prompt fires again, and tmux
    consumes the agent's own OSC 7 rather than forwarding it — so without this
    the terminal stays stuck on whatever directory vv was launched from. We send
    one final OSC 7 for the worktree just before the handover. Keeping it in sync
    *after* the handover is :func:`_setup_cwd_forwarding`'s job.
    """
    if not sys.stdout.isatty():
        return
    try:
        sys.stdout.write(_osc7(cwd))
        sys.stdout.flush()
    except OSError:
        pass


def emit_cwd(cwd: Path) -> None:
    """Write a passthrough-wrapped OSC 7 for ``cwd`` to stdout.

    Invoked as ``vv --emit-cwd`` from the tmux cwd hook (see
    :func:`_setup_cwd_forwarding`) with stdout redirected to the active pane's
    tty, so tmux unwraps the passthrough and forwards the OSC 7 to the outer
    terminal. Unconditional: the hook only ever runs it when a forward is wanted.
    """
    sys.stdout.write(_osc7_passthrough(cwd))
    sys.stdout.flush()


def _osc7(cwd: Path) -> str:
    """Build the OSC 7 sequence reporting ``cwd`` to the terminal."""
    uri = f"file://{socket.gethostname()}{quote(str(cwd))}"
    return f"\033]7;{uri}\a"


def _osc7_passthrough(cwd: Path) -> str:
    """Wrap :func:`_osc7` in tmux's DCS passthrough so tmux relays it.

    tmux swallows a pane's OSC 7 rather than forwarding it, so a hook that wants
    the *outer* terminal to learn the pane's directory must wrap the sequence as
    ``ESC P tmux ; <payload> ESC \\`` with every embedded ESC doubled. Requires
    the session's ``allow-passthrough`` option to be on.
    """
    inner = _osc7(cwd).replace("\033", "\033\033")
    return f"\033Ptmux;{inner}\033\\"
