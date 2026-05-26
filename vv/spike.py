"""Phase 0 spike: embed a live tmux session inside a Textual app.

This is a throwaway proof-of-concept for the TUI redesign. It exists to
answer one question: can we run a real ``tmux attach-session`` inside a
Textual widget, both as a local TUI and (via ``textual-serve``) in a web
browser, well enough to drive an agent CLI from a phone?

Usage::

    # local TUI: attach to an existing tmux session by name
    uv run vv-spike <session-name>

    # web: same app served over HTTP for browser access on the tailnet
    uv run textual-serve "uv run vv-spike <session-name>" --host 0.0.0.0

If no session is given, a throwaway one is created (running ``$SHELL``)
so the spike is self-contained.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid


# textual-terminal 0.3.0 imports textual.app.DEFAULT_COLORS, which was
# removed in modern Textual. We don't use its `default_colors="textual"`
# code path, so a stub is enough to satisfy the import. Drop this once
# we fork or replace the widget in Phase 1.
import textual.app as _textual_app

if not hasattr(_textual_app, "DEFAULT_COLORS"):
    _textual_app.DEFAULT_COLORS = {"dark": None, "light": None}

from textual.app import App, ComposeResult  # noqa: E402
from textual.binding import Binding  # noqa: E402
from textual.containers import Vertical  # noqa: E402
from textual.widgets import Footer, Static  # noqa: E402
from textual_terminal import Terminal  # noqa: E402

from . import tmux_ops  # noqa: E402


SPIKE_SESSION_PREFIX = "vv-spike-"


def _ensure_session(name: str | None) -> tuple[str, bool]:
    """Return ``(session_name, created_by_us)``.

    If ``name`` is given and the session exists, use it. If ``name`` is
    given but missing, create it. If ``name`` is None, mint a throwaway
    one rooted in the current working directory.
    """
    if name and tmux_ops.session_exists(name):
        return name, False

    session = name or f"{SPIKE_SESSION_PREFIX}{uuid.uuid4().hex[:8]}"
    # Use plain tmux directly here rather than tmux_ops.create_session():
    # the spike intentionally avoids the @vv tag so it can't be confused
    # for a real vv worktree session.
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-c", os.getcwd()],
        check=True,
    )
    return session, True


class SpikeApp(App):
    """Single-pane spike: a tmux client embedded inside Textual."""

    CSS = """
    Screen { background: $background; }

    #header {
        height: 1;
        background: $primary;
        color: $text;
        padding: 0 1;
    }

    Terminal {
        height: 1fr;
        border: tall $primary;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+f1", "focus_next", "Release focus", show=False),
    ]

    def __init__(self, session_name: str, owns_session: bool) -> None:
        super().__init__()
        self.session_name = session_name
        self.owns_session = owns_session

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"vv-spike — tmux session: [b]{self.session_name}[/b]   "
                f"(ctrl+q quit · ctrl+f1 release focus)",
                id="header",
            )
            yield Terminal(
                command=f"tmux attach-session -t {self.session_name}",
                default_colors="system",
                id="tmux",
            )
            yield Footer()

    def on_ready(self) -> None:
        self.query_one("#tmux", Terminal).start()

    def on_unmount(self) -> None:
        # Don't tear down sessions the user already had; only ones the
        # spike created itself.
        if self.owns_session and tmux_ops.session_exists(self.session_name):
            try:
                tmux_ops.kill_session(self.session_name)
            except tmux_ops.TmuxError:
                pass


def run() -> None:
    """Console-script entry point: ``vv-spike [session-name]``."""
    name = sys.argv[1] if len(sys.argv) > 1 else None
    session, created = _ensure_session(name)
    SpikeApp(session, owns_session=created).run()


def serve() -> None:
    """Console-script entry point: ``vv-spike-serve [session-name] [--host H] [--port P]``.

    Hosts the spike over HTTP/WebSocket via ``textual-serve`` so a
    browser (on the tailnet, ideally) can drive the same embedded tmux
    session that the local TUI would.
    """
    import argparse

    from textual_serve.server import Server

    parser = argparse.ArgumentParser(prog="vv-spike-serve")
    parser.add_argument("session", nargs="?", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    session, _created = _ensure_session(args.session)
    # textual-serve runs `command` as a subprocess per browser tab; the
    # subprocess just re-invokes us with the resolved session name so
    # both clients (TUI + browser) attach to the same tmux session.
    command = f"{sys.executable} -m vv.spike {session}"
    Server(command=command, host=args.host, port=args.port).serve()


if __name__ == "__main__":
    run()
