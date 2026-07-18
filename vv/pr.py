"""Cached pull-request status for vv sessions, shown on the session cards.

Fetching PR status is a ``gh`` call per repo-backed session — too slow to block
the menu on — so a :class:`Snapshot` serves whatever is already cached
*instantly* and refreshes the rest in a background thread, updating the cards
live as answers arrive. Results are cached on disk keyed by a cheap fingerprint
(the branch name + its HEAD commit); a session is only refetched once its branch
moves (a new commit or a switch). CI checks that finish *without* a new commit
therefore lag until the next push — the deliberate trade for a fast menu.
Everything is best-effort: any failure just shows "no PR".
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import config, gh_ops, git_ops

#: Cap on concurrent ``gh`` subprocesses.
MAX_WORKERS = 8

#: Bump when the cache entry shape changes, to discard stale on-disk caches.
CACHE_VERSION = 1


def session_fingerprint(path: Path) -> str | None:
    """A token that changes when the session's branch or HEAD moves.

    Returns ``None`` for a non-git session (a chat) — those have no PR, so we
    never fetch or cache anything for them.
    """
    try:
        branch = git_ops.current_branch(path)
        head = git_ops.head_commit(path)
    except git_ops.GitError:
        return None
    return f"{branch}@{head}"


class Snapshot:
    """PR status for a set of sessions: instant from cache, refreshed in the background.

    Construct it with ``{key: worktree_path}``; :attr:`cached` returns the PR
    statuses already known (no ``gh`` calls), :attr:`stale_keys` the sessions
    whose status will be refetched, and :meth:`refresh` spawns a daemon thread
    that fetches those in parallel, invoking ``on_result(key, pr)`` as each
    lands and rewriting the on-disk cache (pruned to the current sessions) when
    done. The UI stays fully responsive; cards enrich as answers arrive.
    """

    def __init__(self, sessions: dict[object, Path]) -> None:
        self._paths = dict(sessions)
        self._fingerprints: dict[object, str] = {}
        for key, path in self._paths.items():
            fp = session_fingerprint(path)
            if fp is not None:  # skip non-git sessions entirely
                self._fingerprints[key] = fp
        self._cache = _load_cache()

    def _fresh_entry(self, key: object) -> dict | None:
        """The cache entry for ``key`` iff its fingerprint still matches."""
        entry = self._cache.get(_cache_key(key))
        if entry and entry.get("fingerprint") == self._fingerprints.get(key):
            return entry
        return None

    @property
    def cached(self) -> dict[object, dict]:
        """PR statuses already known from the cache (fingerprint still valid)."""
        out: dict[object, dict] = {}
        for key in self._fingerprints:
            entry = self._fresh_entry(key)
            if entry and entry.get("pr"):
                out[key] = entry["pr"]
        return out

    @property
    def stale_keys(self) -> set[object]:
        """Git-backed sessions whose PR status must be refetched."""
        return {
            key
            for key in self._fingerprints
            if not (self._fresh_entry(key) and "pr" in self._fresh_entry(key))
        }

    def refresh(
        self,
        on_result: Callable[[object, dict | None], None],
        stop: threading.Event | None = None,
    ) -> threading.Thread:
        """Fetch stale statuses in a daemon thread; call ``on_result`` per answer.

        ``on_result(key, pr)`` runs on the worker thread as each fetch completes
        (``pr`` may be ``None`` for "no PR"); it is skipped once ``stop`` is set,
        so a closing menu doesn't touch a torn-down UI. The full cache (fresh
        entries preserved, fetched ones updated) is persisted at the end.
        """
        stale = {key: self._paths[key] for key in self.stale_keys}
        seed = self._seed_cache()

        def worker() -> None:
            if stale:
                with ThreadPoolExecutor(max_workers=min(len(stale), MAX_WORKERS)) as pool:
                    futures = {pool.submit(gh_ops.pr_status, path): key for key, path in stale.items()}
                    for future in as_completed(futures):
                        key = futures[future]
                        try:
                            pr = future.result()
                        except Exception:  # noqa: BLE001 — one bad fetch mustn't sink the rest
                            pr = None
                        seed[_cache_key(key)] = {"fingerprint": self._fingerprints[key], "pr": pr}
                        if stop is None or not stop.is_set():
                            on_result(key, pr)
            _save_cache(seed)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        return thread

    def _seed_cache(self) -> dict[str, dict]:
        """Cache pre-filled with the still-valid entries for every current session.

        Refetched sessions overwrite their slot as answers arrive; this both
        preserves fresh entries and prunes any session no longer present.
        """
        seed: dict[str, dict] = {}
        for key, fp in self._fingerprints.items():
            entry = self._fresh_entry(key)
            seed[_cache_key(key)] = {"fingerprint": fp, "pr": entry.get("pr") if entry else None}
        return seed


def _cache_key(key: object) -> str:
    """Stringify a caller key (e.g. a ``(repo, name)`` tuple) for JSON storage."""
    if isinstance(key, (tuple, list)):
        return "/".join(str(part) for part in key)
    return str(key)


def _load_cache() -> dict[str, dict]:
    """Load the PR cache, or ``{}`` on a missing/unreadable/outdated file."""
    import json

    try:
        with config.pr_cache_file().open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {}
    sessions = data.get("sessions")
    return sessions if isinstance(sessions, dict) else {}


def _save_cache(sessions: dict[str, dict]) -> None:
    """Write the PR cache atomically and best-effort (failures ignored)."""
    import json

    path = config.pr_cache_file()
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump({"version": CACHE_VERSION, "sessions": sessions}, handle)
        tmp.replace(path)
    except OSError:
        pass
