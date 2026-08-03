"""Thin wrapper around the ``gh`` (GitHub CLI) for discovering repos.

Like :mod:`vv.git_ops` / :mod:`vv.tmux_ops` / :mod:`vv.cmux_ops`, every call
shells out to the real CLI. ``gh`` is entirely optional: it is only used to
populate the "Add a new repo" picker with the repositories the signed-in user
can access. Anything that goes wrong (gh missing, not logged in, an API error)
degrades to an empty list so the caller can fall back to manual URL entry
rather than aborting — hence these *discovery* helpers swallow failures instead
of raising.

:func:`create_repo` is the one deliberate exception: it **mutates** GitHub, so a
failure there is a hard error the user must see (there is nothing to fall back
to) and it raises :class:`GhError`, caught centrally in ``cli.main()`` like the
other ops modules' errors.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

#: How long to wait for a single ``gh pr view`` before giving up on it.
PR_TIMEOUT_SECONDS = 6.0

#: Poll budget for :func:`wait_for_commits` — how long to wait for GitHub to
#: finish copying a template's contents into a freshly generated repo.
CONTENT_TIMEOUT_SECONDS = 30.0
CONTENT_POLL_SECONDS = 1.0


class GhError(RuntimeError):
    """A ``gh`` invocation that vv cannot degrade around (repo creation)."""


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


def list_repos_detailed() -> list[tuple[str, bool]]:
    """Return ``(owner/name, is_template)`` for every repo the user can access.

    Backed by the ``user/repos`` API endpoint, which spans the user's own
    repositories plus those of every organization they belong to — broader than
    ``gh repo list`` (which is just the user's own). Paginated through all pages
    and returned sorted + de-duplicated. Any failure yields ``[]`` so the picker
    silently falls back to manual URL entry.

    Responses are cached by ``gh`` for an hour (``--cache``): walking every page
    of a large account is slow, so the first menu open pays for it and the rest
    are instant. A repo created in that window won't appear until the cache
    expires — use "Enter a clone URL" for those. ``--jq`` is applied client-side
    to the cached response, so :func:`list_repos` and :func:`list_template_repos`
    share this one cache entry rather than each paying for their own walk.
    """
    try:
        result = subprocess.run(
            [
                "gh", "api", "--paginate", "--cache", "1h",
                "-X", "GET", "user/repos",
                "-f", "per_page=100",
                "-f", "sort=full_name",
                "--jq", '.[] | [.full_name, (.is_template // false)] | @tsv',
            ],
            capture_output=True,
            text=True,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    repos: dict[str, bool] = {}
    for line in result.stdout.splitlines():
        name, _tab, flag = line.strip().partition("\t")
        if name:
            repos[name] = flag.strip().lower() == "true"
    return sorted(repos.items())


def list_repos() -> list[str]:
    """Return ``owner/name`` for every repo the authenticated user can access."""
    return [name for name, _is_template in list_repos_detailed()]


def list_template_repos() -> list[str]:
    """Return ``owner/name`` for every accessible repo marked as a template.

    Reuses the same cached ``user/repos`` walk as :func:`list_repos`, so opening
    the "Create a new GitHub project" menu right after the "Add a new repo" one
    costs no extra API calls.
    """
    return [name for name, is_template in list_repos_detailed() if is_template]


def list_owners() -> list[str]:
    """Return the accounts the user can create a repo under, personal first.

    That is their own login followed by each organization they belong to
    (sorted). Degrades to ``[]`` if ``gh`` cannot answer — the caller then has
    no owner to offer and abandons the flow.
    """
    login = _api_lines(["-X", "GET", "user", "--jq", ".login"], cache="1h")
    orgs = _api_lines(
        ["--paginate", "-X", "GET", "user/orgs", "-f", "per_page=100", "--jq", ".[].login"],
        cache="1h",
    )
    owners = [o for o in sorted(set(orgs)) if o and o not in login]
    return [*login, *owners]


def _api_lines(args: list[str], *, cache: str | None = None) -> list[str]:
    """Run ``gh api`` and return its non-blank output lines (``[]`` on failure)."""
    command = ["gh", "api"]
    if cache:
        command += ["--cache", cache]
    try:
        result = subprocess.run(command + args, capture_output=True, text=True)
    except OSError:
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def create_repo(
    name_with_owner: str,
    *,
    template: str | None = None,
    private: bool = True,
) -> None:
    """Create ``owner/name`` on GitHub, optionally generated from ``template``.

    Unlike everything else in this module this raises (:class:`GhError`) rather
    than degrading: the user asked for a repo to exist, and silently continuing
    to a clone of something that was never created would be worse than a clear
    failure. ``gh`` requires an explicit visibility in non-interactive mode, so
    ``private`` is always passed one way or the other.
    """
    command = ["gh", "repo", "create", name_with_owner]
    if template:
        command += ["--template", template]
    command.append("--private" if private else "--public")
    try:
        result = subprocess.run(command, capture_output=True, text=True)
    except OSError as exc:
        raise GhError(f"could not run gh: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise GhError(f"could not create '{name_with_owner}': {detail}")


def has_commits(name_with_owner: str) -> bool:
    """Return True once ``owner/name`` has at least one commit.

    The commits endpoint answers 409 for an empty repository, which ``gh``
    surfaces as a non-zero exit — so "did this work" and "is it populated yet"
    collapse into the exit status.

    ``-X GET`` is not optional: ``gh api`` defaults to POST as soon as any ``-f``
    parameter is present, which turns this read into a 404.
    """
    return bool(
        _api_lines([
            "-X", "GET", f"repos/{name_with_owner}/commits",
            "-f", "per_page=1", "--jq", "length",
        ])
    )


def wait_for_commits(
    name_with_owner: str,
    *,
    timeout: float = CONTENT_TIMEOUT_SECONDS,
    interval: float = CONTENT_POLL_SECONDS,
) -> bool:
    """Poll until a freshly generated repo has content, or ``timeout`` elapses.

    Generating a repo from a template returns before GitHub has finished copying
    the template's files in, so an immediate clone can come back empty — and vv
    would then "bootstrap" the unborn HEAD with a seed commit and push a history
    that diverges from the content landing behind it. Waiting for the first
    commit to exist closes that window.

    Returns False on timeout so the caller can warn rather than hang forever.
    """
    deadline = time.monotonic() + timeout
    while True:
        if has_commits(name_with_owner):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


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
