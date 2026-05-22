"""Filesystem locations and on-disk settings used by vv.

These can be overridden with environment variables:

    WORKSPACES_DIR  where bare/primary clones live (one per repo)
    WORKTREES_DIR   where per-session worktrees live
    VV_CONFIG       path to the TOML config file
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when the config file exists but cannot be read or parsed."""


DEFAULT_WORKSPACES_DIR = Path.home() / ".vv" / "workspaces"
DEFAULT_WORKTREES_DIR = Path.home() / ".vv" / "worktrees"
DEFAULT_CONFIG_FILE = Path.home() / ".vv" / "config.toml"


def workspaces_dir() -> Path:
    """Directory holding the primary clone of each repo."""
    raw = os.environ.get("WORKSPACES_DIR")
    path = Path(raw).expanduser() if raw else DEFAULT_WORKSPACES_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def worktrees_dir() -> Path:
    """Directory holding per-session worktrees, grouped by repo name."""
    raw = os.environ.get("WORKTREES_DIR")
    path = Path(raw).expanduser() if raw else DEFAULT_WORKTREES_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_file() -> Path:
    """Path to the vv TOML config file (override with ``VV_CONFIG``)."""
    raw = os.environ.get("VV_CONFIG")
    return Path(raw).expanduser() if raw else DEFAULT_CONFIG_FILE


def _load_config() -> dict:
    """Parse the config file, or return ``{}`` when there is none.

    Raises :class:`ConfigError` if the file exists but is unreadable or not
    valid TOML — a silent fallback would hide the user's typo.
    """
    path = config_file()
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"could not read config file {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in config file {path}: {exc}") from exc


def configured_agent() -> str | None:
    """Return the ``agent`` set in the config file, if any."""
    value = _load_config().get("agent")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def configured_ask() -> bool:
    """Return the ``ask`` setting from the config file (default False).

    When True, vv launches agents with their normal permission prompts rather
    than bypassing them. A non-boolean value is ignored.
    """
    value = _load_config().get("ask", False)
    return value if isinstance(value, bool) else False
