"""Tests for remote-launcher orchestration (cmux ssh workspace + send)."""

from __future__ import annotations

import shlex

import pytest

from vv import config, remote


def _login_argv(text: str) -> list[str]:
    """Parse the sent text one shell level: the ``bash -lc '<vv …>'`` wrapper.

    ``send_text`` appends a literal ``\\n`` (cmux's submit escape); strip it
    before splitting so it doesn't leak into the parsed argv.
    """
    return shlex.split(text.removesuffix("\\n"))


def _remote_argv(text: str) -> list[str]:
    """Parse both shell levels: the vv command as the remote shell sees it.

    Descends ``bash -lc`` → the vv invocation that the login shell runs.
    """
    return shlex.split(_login_argv(text)[-1])


@pytest.fixture
def captured(monkeypatch):
    """Stub cmux so nothing real happens; yield the captured ssh + send calls."""
    seen: dict = {}
    monkeypatch.setattr(remote.cmux_ops, "ensure_available", lambda: None)

    def fake_new_ssh(target, *, name=None, port=None, identity=None, ssh_options=()):
        seen.update(
            target=target, name=name, port=port,
            identity=identity, ssh_options=ssh_options,
        )
        return "ws-1"

    monkeypatch.setattr(remote.cmux_ops, "new_ssh_workspace", fake_new_ssh)
    monkeypatch.setattr(
        remote.cmux_ops,
        "send_text",
        lambda workspace_id, text: seen.update(workspace_id=workspace_id, text=text),
    )
    return seen


def test_opens_ssh_workspace_then_sends_the_vv_command(captured):
    cfg = config.Remote(host="myserver")
    remote.launch(cfg, remote_argv=["--local"], title="otter")
    # The native SSH workspace is opened for the bare target, titled to match.
    assert captured["target"] == "myserver"
    assert captured["name"] == "otter"
    # The vv command is sent to *that* workspace, login-wrapped and submitted.
    assert captured["workspace_id"] == "ws-1"
    assert captured["text"].endswith("\\n")
    assert _login_argv(captured["text"]) == ["bash", "-lc", "vv --local"]
    assert _remote_argv(captured["text"]) == ["vv", "--local"]


def test_user_makes_the_ssh_target(captured):
    cfg = config.Remote(host="box", user="matt")
    remote.launch(cfg, remote_argv=["--local"], title="box")
    assert captured["target"] == "matt@box"


def test_port_identity_and_ssh_options_are_forwarded(captured):
    cfg = config.Remote(
        host="box",
        port=2222,
        identity="~/.ssh/id_ed25519",
        ssh_options=("StrictHostKeyChecking=no",),
    )
    remote.launch(cfg, remote_argv=["--local"], title="box")
    assert captured["port"] == 2222
    assert captured["identity"] == "~/.ssh/id_ed25519"
    assert captured["ssh_options"] == ("StrictHostKeyChecking=no",)


def test_custom_vv_command_is_used(captured):
    cfg = config.Remote(host="h", vv_command="/opt/bin/vv")
    remote.launch(cfg, remote_argv=["--local"], title="h")
    assert _remote_argv(captured["text"]) == ["/opt/bin/vv", "--local"]


def test_quoting_survives_a_url_with_shell_specials(captured):
    cfg = config.Remote(host="h")
    url = "https://example.com/o/r.git?x=1&y=2"
    remote.launch(cfg, remote_argv=["--name", "otter", "--local", url], title="otter")
    # The URL's & and ? must reach the remote vv intact — not be re-split or
    # interpreted by the send escaping or the remote shell.
    assert _remote_argv(captured["text"]) == [
        "vv", "--name", "otter", "--local", url
    ]


def test_launch_aborts_without_a_workspace_id(monkeypatch):
    monkeypatch.setattr(remote.cmux_ops, "ensure_available", lambda: None)
    monkeypatch.setattr(
        remote.cmux_ops, "new_ssh_workspace", lambda *a, **k: None
    )
    monkeypatch.setattr(
        remote.cmux_ops, "send_text",
        lambda *a, **k: pytest.fail("send_text must not run without a workspace id"),
    )
    with pytest.raises(remote.cmux_ops.CmuxError, match="workspace id"):
        remote.launch(config.Remote(host="h"), remote_argv=["--local"], title="h")


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
        remote.cmux_ops, "new_ssh_workspace",
        lambda *a, **k: pytest.fail("new_ssh_workspace must not run"),
    )
    with pytest.raises(remote.cmux_ops.CmuxError):
        remote.launch(config.Remote(host="h"), remote_argv=["--local"], title="h")
