"""Remote-launcher mode: run vv on a server, surfaced as a local cmux tab.

In remote mode local vv does no git/tmux work of its own. It opens a native
cmux SSH workspace to the configured server, then types ``vv`` into it —
forwarding the user's arguments verbatim — so bare ``vv`` lands you in the
remote's own interactive TUI and ``vv <url>`` runs the remote create flow. The
worktree/tmux/agent session is created entirely on the remote, "as usual".

Two cmux calls, not one: ``cmux ssh`` opens the workspace (a first-class remote
connection — cmuxd-remote install, agent notifications, reconnect), and a
follow-up ``cmux send`` types the ``vv`` command into it once the remote shell's
prompt has appeared (see :func:`cmux_ops.wait_until_ready` — sending mid-startup
loses the keystrokes). We deliberately don't pass the command as a trailing
``ssh`` argument: cmux disables its remote bootstrap when a remote command is
present, which would forfeit exactly those integrations. See
:func:`cmux_ops.new_ssh_workspace`.

This module is a dumb pipe: :func:`cli.main` decides the remote argv and the tab
title; we build the ``vv`` command line and hand both to cmux.
"""

from __future__ import annotations

import shlex

import typer

from . import cmux_ops, config, names


def gen_name() -> str:
    """Pick a fresh session name, avoiding existing cmux workspace titles.

    Used when local vv knows a session is being created up front, so the local
    tab can be titled to match the remote session it spawns.
    """
    return names.random_name(cmux_ops.list_workspace_titles())


def _ssh_target(remote: config.Remote) -> str:
    """Return the ``[user@]host`` SSH destination for ``remote``."""
    return f"{remote.user}@{remote.host}" if remote.user else remote.host


def _remote_command(remote: config.Remote, remote_argv: list[str]) -> str:
    """Build the ``vv`` command line typed into the remote shell.

    The invocation is run through a login shell (``bash -lc``) so the remote's
    profile is sourced: cmux's interactive remote shell is not guaranteed to be
    a login shell, and PATH additions like ``~/.local/bin`` (where ``uv tool
    install`` puts ``vv``) live in ``~/.profile`` — without them the command is
    "not found". The ``vv`` argv is collapsed to one shlex-quoted token so its
    spaces (and any ``&``/``?`` in a URL) survive the remote shell intact.
    """
    vv_invocation = shlex.join([remote.vv_command, *remote_argv])
    return shlex.join(["bash", "-lc", vv_invocation])


def launch(remote: config.Remote, *, remote_argv: list[str], title: str) -> None:
    """Open a cmux SSH workspace on ``remote`` and run ``vv`` in it."""
    cmux_ops.ensure_available()

    target = _ssh_target(remote)
    typer.secho(
        f"Opening cmux SSH workspace '{title}' → {target}...",
        fg=typer.colors.CYAN,
    )
    workspace_id = cmux_ops.new_ssh_workspace(
        target,
        name=title,
        port=remote.port,
        identity=remote.identity,
        ssh_options=remote.ssh_options,
    )
    if not workspace_id:
        raise cmux_ops.CmuxError(
            "cmux ssh did not report a workspace id, so the vv command can't be "
            "sent to the new workspace"
        )

    # Wait for the remote shell's prompt before typing: a just-opened cmux ssh
    # workspace is still connecting, and characters sent into it during startup
    # (the submitting Enter especially) get swallowed, leaving the command typed
    # but unrun. Polling beats blind type-ahead. On timeout we send anyway — no
    # worse than firing immediately.
    if not cmux_ops.wait_until_ready(
        workspace_id,
        delay=remote.ready_delay,
        timeout=remote.ready_timeout,
        interval=remote.ready_interval,
    ):
        typer.secho(
            "  (remote shell not detected as ready; sending anyway)",
            fg=typer.colors.YELLOW,
        )

    command = _remote_command(remote, remote_argv)
    # A literal "\n" tells cmux to submit the line (it becomes a carriage
    # return).
    cmux_ops.send_text(workspace_id, f"{command}\\n")
    typer.secho(f"  remote:  {target}", fg=typer.colors.GREEN)
    typer.secho(f"  command: {command}", fg=typer.colors.GREEN)
