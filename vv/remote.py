"""Remote-launcher mode: run vv on a server, surfaced as a local cmux tab.

In remote mode local vv does no git/tmux work of its own. It opens a cmux
workspace whose command SSHes into the configured server and runs ``vv`` there,
forwarding the user's arguments verbatim — so bare ``vv`` lands you in the
remote's own interactive TUI and ``vv <url>`` runs the remote create flow. The
worktree/tmux/agent session is created entirely on the remote, "as usual".

This module is a dumb pipe: :func:`cli.main` decides the remote argv and the tab
title; we just build the ``ssh`` invocation and hand it to cmux.
"""

from __future__ import annotations

import shlex
from pathlib import Path

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


def _command_string(remote: config.Remote, remote_argv: list[str]) -> str:
    """Build the shell command cmux runs: ``ssh -t … <host> '<vv …>'``.

    The remote vv command is collapsed to a single shlex-quoted argument so it
    survives three layers of parsing — the cmux pane's shell, ``ssh``, then the
    remote shell — without re-splitting on spaces in URLs or args.
    """
    remote_command = shlex.join([remote.vv_command, *remote_argv])
    ssh_argv = ["ssh", "-t", *remote.ssh_options]
    if remote.port is not None:
        ssh_argv += ["-p", str(remote.port)]
    ssh_argv += [_ssh_target(remote), remote_command]
    return shlex.join(ssh_argv)


def launch(remote: config.Remote, *, remote_argv: list[str], title: str) -> None:
    """Open a cmux workspace that SSHes to ``remote`` and runs ``vv``."""
    cmux_ops.ensure_available()

    command = _command_string(remote, remote_argv)
    cwd = Path(remote.cwd).expanduser() if remote.cwd else Path.cwd()

    typer.secho(
        f"Opening cmux workspace '{title}' → {_ssh_target(remote)}...",
        fg=typer.colors.CYAN,
    )
    cmux_ops.new_workspace(cwd, command, title=title)
    typer.secho(f"  remote:  {_ssh_target(remote)}", fg=typer.colors.GREEN)
    typer.secho(f"  command: {command}", fg=typer.colors.GREEN)
