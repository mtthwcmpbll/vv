"""Install vv's bundled agent skills into the agent CLIs found on this machine.

vv ships skills (`vv/_skills/<name>/SKILL.md`) that are useful in any vv session
— the `pr` skill above all, since committing and opening a PR is how a session
ends. They are only useful once the agent can *see* them, and every agent looks
in its own place, so :func:`install` copies a skill into each tool's personal
skills directory.

The five supported tools happen to agree on the format — a `<root>/skills/
<name>/SKILL.md` directory, Anthropic's Agent Skills convention — so this is a
copy, not a translation. Only the root differs, which is all :data:`_ROOTS`
records. A tool is "found" when its config root exists; vv creates the `skills/`
directory under it but never the root itself, so a tool that was never installed
doesn't get a stray config dir invented for it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

#: Directory of skills shipped inside the package. Underscored so it can never
#: be mistaken for — or shadow — this module on import: `vv/skills/` next to
#: `vv/skills.py` resolves only by the namespace-package tiebreak.
BUNDLED_DIR = Path(__file__).parent / "_skills"

#: The one file that makes a directory a skill, for every tool below.
SKILL_FILE = "SKILL.md"

#: ``key -> (label, env var overriding the root, root relative to $HOME)``.
#: Roots are resolved lazily in :func:`targets` so the environment (and tests'
#: patched ``HOME``) is read at call time rather than at import.
_ROOTS: tuple[tuple[str, str, str | None, str], ...] = (
    ("claude", "Claude Code", "CLAUDE_CONFIG_DIR", ".claude"),
    ("cursor", "Cursor", None, ".cursor"),
    ("copilot", "GitHub Copilot", None, ".copilot"),
    ("codex", "Codex", "CODEX_HOME", ".codex"),
    # Antigravity reads global skills from ~/.gemini/config/skills — the one
    # location all three of its flavors (IDE, CLI, agy) agree on. Its other
    # ~/.gemini/antigravity dirs are conversation state, not configuration.
    ("antigravity", "Antigravity", None, ".gemini/config"),
)


@dataclass(frozen=True)
class Target:
    """An agent tool vv can install skills into."""

    key: str
    label: str
    root: Path

    @property
    def skills_dir(self) -> Path:
        """Where this tool looks for personal skills."""
        return self.root / "skills"

    def path_for(self, skill: str) -> Path:
        """Where ``skill`` would be installed for this tool."""
        return self.skills_dir / skill


def targets() -> tuple[Target, ...]:
    """Every tool vv knows how to install into, whether present or not."""
    home = Path.home()
    resolved = []
    for key, label, env_var, relative in _ROOTS:
        override = os.environ.get(env_var) if env_var else None
        root = Path(override).expanduser() if override else home / relative
        resolved.append(Target(key, label, root))
    return tuple(resolved)


def discovered() -> tuple[Target, ...]:
    """The tools actually installed here — those whose config root exists."""
    return tuple(t for t in targets() if t.root.is_dir())


def bundled_skills() -> tuple[str, ...]:
    """Names of the skills shipped with vv, sorted."""
    if not BUNDLED_DIR.is_dir():
        return ()
    return tuple(sorted(
        p.name for p in BUNDLED_DIR.iterdir()
        if p.is_dir() and (p / SKILL_FILE).is_file()
    ))


def _digest(directory: Path) -> str:
    """A content hash of a skill directory — its file tree and every byte in it.

    Skills may carry `scripts/` and `references/` alongside `SKILL.md`, so
    "already installed and identical" has to mean the whole tree, not one file.
    """
    sha = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        sha.update(str(path.relative_to(directory)).encode())
        sha.update(b"\0")
        sha.update(path.read_bytes())
        sha.update(b"\0")
    return sha.hexdigest()


def status(skill: str, target: Target) -> str:
    """``"missing"``, ``"same"``, or ``"differs"`` for ``skill`` at ``target``.

    ``"differs"`` covers both a stale copy from an older vv and one the user
    edited in place — indistinguishable from here, which is why installing over
    it is a decision for the caller to confirm rather than make.
    """
    installed = target.path_for(skill)
    if not (installed / SKILL_FILE).is_file():
        return "missing"
    if _digest(installed) == _digest(BUNDLED_DIR / skill):
        return "same"
    return "differs"


def install(skill: str, target: Target) -> Path:
    """Copy ``skill`` into ``target``, replacing whatever is there.

    Returns the installed path. Creates the tool's `skills/` directory (but
    never its root — see the module docstring).
    """
    source = BUNDLED_DIR / skill
    if not (source / SKILL_FILE).is_file():
        raise FileNotFoundError(f"no bundled skill named {skill!r}")
    destination = target.path_for(skill)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Replace rather than merge: a file dropped from the skill since the last
    # install would otherwise linger and keep the tree looking "differs".
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    return destination


__all__ = [
    "BUNDLED_DIR",
    "SKILL_FILE",
    "Target",
    "bundled_skills",
    "discovered",
    "install",
    "status",
    "targets",
]