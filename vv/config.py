"""Filesystem locations and on-disk settings used by vv.

These can be overridden with environment variables:

    WORKSPACES_DIR  where bare/primary clones live (one per repo)
    WORKTREES_DIR   where per-session worktrees live
    VV_CONFIG       path to the TOML config file
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when the config file exists but cannot be read or parsed."""


@dataclass(frozen=True)
class Remote:
    """A remote server vv launches sessions on, in remote-launcher mode.

    See :mod:`vv.remote`. Everything here describes the SSH target and how to
    invoke vv on the far side; ``ssh_options`` are cmux ``--ssh-option`` values
    (``-o Key=Value`` passthrough) and ``identity`` an SSH key path.
    ``ready_delay`` is an unconditional wait before polling begins (use it when
    you know login takes a second or two); ``ready_timeout`` / ``ready_interval``
    tune how long (and how often) to then poll the freshly-opened workspace for
    its shell prompt before typing the ``vv`` command into it (see
    :func:`vv.cmux_ops.wait_until_ready`).
    """

    host: str
    user: str | None = None
    port: int | None = None
    identity: str | None = None
    ssh_options: tuple[str, ...] = ()
    vv_command: str = "vv"
    ready_delay: float = 0.0
    ready_timeout: float = 20.0
    ready_interval: float = 0.4


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


def chats_dir() -> Path:
    """Directory holding chat-only sessions (no repo, no git worktree).

    Lives under :func:`worktrees_dir` as a sentinel ``_chats`` namespace; the
    leading underscore guarantees it cannot collide with a real repo name.
    """
    path = worktrees_dir() / "_chats"
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


def configured_mode() -> str:
    """Return the launcher mode: ``"remote"`` or (default) ``"local"``.

    Only the exact string ``"remote"`` enables remote-launcher mode; any other
    value — including a missing key or a typo — keeps vv fully local so an
    unconfigured machine behaves exactly as before.
    """
    return "remote" if _load_config().get("mode") == "remote" else "local"


def configured_remote() -> Remote | None:
    """Parse the ``[remote]`` table into a :class:`Remote`, or ``None``.

    Raises :class:`ConfigError` if the table exists but is missing ``host`` (or
    carries a malformed ``port``), since a half-configured remote would fail
    obscurely later.
    """
    table = _load_config().get("remote")
    if not isinstance(table, dict):
        return None

    host = table.get("host")
    if not isinstance(host, str) or not host.strip():
        raise ConfigError("config [remote] is missing a 'host' value")

    port = table.get("port")
    if port is not None and not isinstance(port, int):
        raise ConfigError("config [remote] 'port' must be an integer")

    user = table.get("user")
    identity = table.get("identity")
    vv_command = table.get("vv_command")

    raw_options = table.get("ssh_options", [])
    if not isinstance(raw_options, list) or not all(
        isinstance(opt, str) for opt in raw_options
    ):
        raise ConfigError("config [remote] 'ssh_options' must be a list of strings")

    defaults = Remote(host=host)
    ready_delay = _seconds(table, "ready_delay", defaults.ready_delay, allow_zero=True)
    ready_timeout = _seconds(table, "ready_timeout", defaults.ready_timeout)
    ready_interval = _seconds(table, "ready_interval", defaults.ready_interval)

    return Remote(
        host=host.strip(),
        user=user.strip() if isinstance(user, str) and user.strip() else None,
        port=port,
        identity=identity.strip()
        if isinstance(identity, str) and identity.strip()
        else None,
        ssh_options=tuple(raw_options),
        vv_command=vv_command.strip()
        if isinstance(vv_command, str) and vv_command.strip()
        else "vv",
        ready_delay=ready_delay,
        ready_timeout=ready_timeout,
        ready_interval=ready_interval,
    )


def _seconds(table: dict, key: str, default: float, *, allow_zero: bool = False) -> float:
    """Read ``table[key]`` as a number of seconds, or ``default`` if unset.

    With ``allow_zero`` the value may be ``0`` (a disabled delay); otherwise it
    must be strictly positive. Booleans are rejected (``bool`` is an ``int``
    subclass in Python, so they'd slip through a plain numeric check).
    """
    value = table.get(key)
    if value is None:
        return default
    invalid = (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (value < 0 if allow_zero else value <= 0)
    )
    if invalid:
        wanted = "non-negative" if allow_zero else "positive"
        raise ConfigError(f"config [remote] '{key}' must be a {wanted} number")
    return float(value)
