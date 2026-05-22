"""Agentic CLI tools vv can launch inside a session.

The "agent" is just the command vv types into the freshly created tmux
session, so *anything* on your ``PATH`` works. ``KNOWN_AGENTS`` below only
seeds the interactive picker — it is a convenience list, not a restriction.
"""

from __future__ import annotations

import shutil

#: The agent launched when nothing else is configured.
DEFAULT_AGENT = "claude"

#: Commands for agent CLIs vv knows about, in menu order. Edit freely: the
#: picker shows whichever of these are found on ``PATH``, and the user can
#: always type a command that is not listed here.
KNOWN_AGENTS: tuple[str, ...] = (
    "claude",   # Anthropic Claude Code
    "codex",    # OpenAI Codex CLI
    "gemini",   # Google Gemini CLI
    "copilot",  # GitHub Copilot CLI
    "agy",      # agy-cli
)


def _command_of(agent: str) -> str:
    """Return the executable name from an agent string (which may have args)."""
    parts = agent.split()
    return parts[0] if parts else agent


def installed_agents() -> list[str]:
    """Return the known agent commands that are present on ``PATH``."""
    return [a for a in KNOWN_AGENTS if shutil.which(a) is not None]


def is_installed(agent: str) -> bool:
    """Return True if ``agent``'s executable resolves on ``PATH``.

    Only the first token is checked, so ``"claude --foo"`` tests ``claude``.
    """
    return shutil.which(_command_of(agent)) is not None
