"""Thin wrapper around the ``gh`` (GitHub CLI) for discovering repos.

Like :mod:`vv.git_ops` / :mod:`vv.tmux_ops` / :mod:`vv.cmux_ops`, every call
shells out to the real CLI. ``gh`` is entirely optional: it is only used to
populate the "Add a new repo" picker with the repositories the signed-in user
can access. Anything that goes wrong (gh missing, not logged in, an API error)
degrades to an empty list so the caller can fall back to manual URL entry
rather than aborting — hence these helpers swallow failures instead of raising.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

#: How long to wait for a single ``gh pr view`` before giving up on it.
PR_TIMEOUT_SECONDS = 6.0


def is_available() -> bool:
    """Return True if ``gh`` is on PATH and authenticated.

    Both conditions matter: an installed-but-logged-out ``gh`` cannot list
    private repos, so we treat it as unavailable and let vv prompt for a URL.
    """
    if shutil.which("gh") is None:
        return False
    result = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def list_repos() -> list[str]:
    """Return ``owner/name`` for every repo the authenticated user can access.

    Backed by the ``user/repos`` API endpoint, which spans the user's own
    repositories plus those of every organization they belong to — broader than
    ``gh repo list`` (which is just the user's own). Paginated through all pages
    and returned sorted + de-duplicated. Any failure yields ``[]`` so the picker
    silently falls back to manual URL entry.

    Responses are cached by ``gh`` for an hour (``--cache``): walking every page
    of a large account is slow, so the first menu open pays for it and the rest
    are instant. A repo created in that window won't appear until the cache
    expires — use "Enter a clone URL" for those.
    """
    try:
        result = subprocess.run(
            [
                "gh", "api", "--paginate", "--cache", "1h",
                "-X", "GET", "user/repos",
                "-f", "per_page=100",
                "-f", "sort=full_name",
                "--jq", ".[].full_name",
            ],
            capture_output=True,
            text=True,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    names = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return sorted(names)


def pr_status(worktree_path: Path) -> dict | None:
    """Return the pull-request status for the branch checked out in a worktree.

    Runs ``gh pr view`` inside ``worktree_path`` and normalizes the result to
    ``{"number": int, "state": "open"|"merged"|"closed", "checks":
    "passing"|"failing"|"pending"|None}``. Returns ``None`` when there is no PR
    for the branch, the directory is not a GitHub repo, or ``gh`` is missing /
    fails — in the never-raise spirit of this module, so callers can just show
    "no PR".
    """
    try:
        result = subprocess.run(
            [
                "gh", "pr", "view",
                "--json", "number,state,isDraft,statusCheckRollup",
            ],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=PR_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None  # no PR for this branch, or not a GitHub repo
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("number") is None:
        return None
    # `state` is OPEN/MERGED/CLOSED; a draft is an open PR with isDraft set, so
    # promote it to its own "draft" state for the caller.
    state = str(data.get("state", "")).lower() or "open"
    if state == "open" and data.get("isDraft"):
        state = "draft"
    return {
        "number": data.get("number"),
        "state": state,
        "checks": _rollup_state(data.get("statusCheckRollup")),
    }


def _rollup_state(rollup: object) -> str | None:
    """Collapse a ``statusCheckRollup`` list into passing/failing/pending/None.

    Handles both check-run entries (``status`` + ``conclusion``) and legacy
    status contexts (``state``). Failing wins over pending wins over passing; a
    PR with no checks at all yields ``None``.
    """
    if not isinstance(rollup, list) or not rollup:
        return None
    failing = {"FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "CANCELLED"}
    pending = {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "EXPECTED", "REQUESTED"}
    saw_pending = False
    for check in rollup:
        if not isinstance(check, dict):
            continue
        # Check runs report a terminal `conclusion` only once `status` completes;
        # status contexts carry a single `state`.
        signal = (check.get("conclusion") or check.get("status") or check.get("state") or "").upper()
        if signal in failing:
            return "failing"
        if signal in pending:
            saw_pending = True
    return "pending" if saw_pending else "passing"


def clone_url(name_with_owner: str, protocol: str = "ssh") -> str:
    """Map an ``owner/name`` selection to a github.com clone URL.

    ``protocol`` selects the URL form — ``"ssh"`` (the default) produces
    ``git@github.com:owner/name.git``; anything else produces the HTTPS form.
    The caller resolves it from the config (see
    :func:`config.configured_clone_protocol`).
    """
    if protocol == "ssh":
        return f"git@github.com:{name_with_owner}.git"
    return f"https://github.com/{name_with_owner}.git"
