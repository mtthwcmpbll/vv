"""Tests for remote-launcher orchestration (ssh + cmux command building)."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from vv import config, remote


def _ssh_argv(command: str) -> list[str]:
    """Parse the cmux command string one shell level: the ssh argv."""
    return shlex.split(command)


def _remote_argv(command: str) -> list[str]:
    """Parse both shell levels: the vv command as the remote shell sees it."""
    return shlex.split(_ssh_argv(command)[-1])


@pytest.fixture
def captured_workspace(monkeypatch):
    """Stub cmux so no real workspace is created; yield the captured call."""
    seen: dict = {}
    monkeypatch.setattr(remote.cmux_ops, "ensure_available", lambda: None)
    monkeypatch.setattr(
        remote.cmux_ops,
        "new_workspace",
        lambda cwd, command, title=None: seen.update(
            cwd=cwd, command=command, title=title
        ),
    )
    return seen


def test_plain_host_builds_a_simple_ssh_command(captured_workspace):
    cfg = config.Remote(host="myserver")
    remote.launch(cfg, remote_argv=["--local"], title="myserver")
    command = captured_workspace["command"]
    # ssh receives the host then the whole vv command as one argument.
    assert _ssh_argv(command) == ["ssh", "-t", "myserver", "vv --local"]
    assert _remote_argv(command) == ["vv", "--local"]
    assert captured_workspace["title"] == "myserver"


def test_user_port_and_ssh_options_are_included(captured_workspace):
    cfg = config.Remote(
        host="box",
        user="matt",
        port=2222,
        ssh_options=("-i", "~/.ssh/id_ed25519"),
    )
    remote.launch(cfg, remote_argv=["--local"], title="box")
    assert _ssh_argv(captured_workspace["command"]) == [
        "ssh", "-t", "-i", "~/.ssh/id_ed25519", "-p", "2222", "matt@box", "vv --local"
    ]


def test_custom_vv_command_is_used(captured_workspace):
    cfg = config.Remote(host="h", vv_command="/opt/bin/vv")
    remote.launch(cfg, remote_argv=["--local"], title="h")
    assert _remote_argv(captured_workspace["command"]) == ["/opt/bin/vv", "--local"]


def test_quoting_survives_a_url_with_shell_specials(captured_workspace):
    cfg = config.Remote(host="h")
    url = "https://example.com/o/r.git?x=1&y=2"
    remote.launch(
        cfg, remote_argv=["--name", "otter", "--local", url], title="otter"
    )
    # The URL's & and ? must reach the remote vv intact — not be re-split or
    # interpreted by the cmux-pane shell, ssh, or the remote shell.
    assert _remote_argv(captured_workspace["command"]) == [
        "vv", "--name", "otter", "--local", url
    ]


def test_cwd_defaults_to_current_dir(captured_workspace, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg = config.Remote(host="h")
    remote.launch(cfg, remote_argv=["--local"], title="h")
    assert captured_workspace["cwd"] == tmp_path


def test_cwd_honors_remote_config(captured_workspace):
    cfg = config.Remote(host="h", cwd="~/code")
    remote.launch(cfg, remote_argv=["--local"], title="h")
    assert captured_workspace["cwd"] == Path("~/code").expanduser()


def test_gen_name_avoids_existing_workspace_titles(monkeypatch):
    monkeypatch.setattr(
        remote.cmux_ops, "list_workspace_titles", lambda: ["falcon", "otter"]
    )
    seen: dict = {}

    def fake_random(taken):
        seen["taken"] = set(taken)
        return "raven"

    monkeypatch.setattr(remote.names, "random_name", fake_random)
    assert remote.gen_name() == "raven"
    assert seen["taken"] == {"falcon", "otter"}


def test_launch_aborts_when_cmux_missing(monkeypatch):
    def boom():
        raise remote.cmux_ops.CmuxError("cmux is not installed")

    monkeypatch.setattr(remote.cmux_ops, "ensure_available", boom)
    monkeypatch.setattr(
        remote.cmux_ops, "new_workspace",
        lambda *a, **k: pytest.fail("new_workspace must not run"),
    )
    with pytest.raises(remote.cmux_ops.CmuxError):
        remote.launch(config.Remote(host="h"), remote_argv=["--local"], title="h")
