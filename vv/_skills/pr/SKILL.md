---
name: pr
description: "Commit the current branch's changes, push them, and open a pull request against the resolved target branch. Auto-detects the branch, remote, PR target, and any PR template. Use when the user says \"/pr\", \"open a PR\", \"create a pull request\", or \"ship this\"."
---

# pr

Commit, push, and open a pull request for the current branch's work. Works in any
git repository — detect the specifics rather than assuming them.

## Preconditions

* If the project defines a more specific PR skill or command, invoke that instead — its instructions take precedence over this one.
* Confirm you are inside a git repository (`git rev-parse --is-inside-work-tree`). If not, tell the user and stop.
* This skill commits and pushes. Only push and open the PR when the user has asked for a PR (that is the trigger for this skill). Never post PR comments or reviews without separate explicit approval.

## Step 1: Gather context

Run these to learn the environment instead of hardcoding anything:

```shell
git status --short                                  # what's uncommitted
git branch --show-current                           # current branch
git remote get-url origin                           # the remote (may be named other than origin)
```

If the current branch is also the default branch, do not commit onto it — ask the user for a feature branch name and create it first with `git checkout -b <name>`.

### Determining the PR target (base) branch

The target is your **merge intent**, which is not always the remote's default branch (stacked PRs, release branches, and `develop`-style workflows all differ). `origin/HEAD` only records the remote's default branch as of clone time — it is a repo-global ref shared by every worktree, is not refreshed when the remote default changes, and may be absent. So use it only as a last resort. Resolve the target in this priority order:

1. **Explicit intent** — a target the user names, or one the environment provides (for example, a Conductor workspace states its target branch in context; honor that verbatim).
2. **Let `gh` resolve it** — running `gh pr create` *without* `--base` defaults to the base repo's default branch via the GitHub API (current and authoritative). Good default, but remember default ≠ always your intended base.
3. **`origin/HEAD`** — heuristic fallback: `git symbolic-ref --quiet refs/remotes/origin/HEAD`, or `git remote show origin | sed -n 's/.*HEAD branch: //p'`. May be stale or missing.
4. **Ask the user** if none of the above is trustworthy.

## Step 2: Review and commit

* Run `git diff` (and `git diff --staged`) to review uncommitted changes. Run `git status` to catch untracked files that should be included.
* Stage the intended files and commit. Follow any commit-message conventions the user gave you or that the repo clearly follows (check `git log` for style). Otherwise write a concise, imperative subject under ~72 characters plus a short body explaining the why.
* If the repo has pre-commit validation (build, lint, tests) documented in `CLAUDE.md`/`README`/`CONTRIBUTING`, run it before committing and fix issues first.

## Step 2.5: Rename the branch to `<type>/<short-summary>`

Generated branch names (`brave-falcon`, `session-3`) say nothing about the work.
Before pushing, rename the local branch so the remote branch reads well to a human.

**Skip the rename entirely if any of these hold** — say which one, and continue to Step 3:

* The branch already matches `^(feat|fix|chore)/[a-z0-9][a-z0-9-]*$` (a re-run of this skill).
* The branch already has an upstream (`git rev-parse --abbrev-ref --symbolic-full-name @{u}`
  succeeds). It has been pushed, and may already be a PR head — renaming now would
  orphan the remote branch and detach the open PR.
* The user named the branch themselves, or asked for a specific name.

Otherwise pick the two parts from the commit(s) you just made and the branch diff:

* **`<type>`** — `feat` for new user-visible behavior, `fix` for a bug fix, `chore`
  for everything else (refactors, deps, docs, tests, tooling, config). When a branch
  mixes types, use the one covering the bulk of the diff.
* **`<short-summary>`** — a kebab-case slug of the work: lowercase `[a-z0-9-]` only,
  2-5 words, under 40 characters, no leading/trailing/doubled `-`. Describe the
  change, not the files (`fix/stale-pr-cache`, not `fix/update-pr-py`). Omit ticket
  IDs unless the repo's `git log` clearly uses them.

Then rename, from inside the worktree:

```shell
git branch -m feat/session-card-labels
```

If that fails because the name is taken, append a short disambiguator (`-2`, or a
distinguishing word) and retry — do not delete the existing branch. The worktree
*directory* keeps its old name; that is expected, leave it alone.

## Step 3: Push

Push the current branch and set upstream if it has none:

```shell
git push -u origin HEAD
```

Use the actual remote name from Step 1 if it isn't `origin`. `HEAD` resolves to the
renamed branch from Step 2.5 — do not reuse a branch name you captured in Step 1.

## Step 4: Review the full PR diff

Compare the whole branch against the target branch from Step 1 — not just this session's edits — so the PR description covers everything reviewers will see:

```shell
git diff <target-branch>...HEAD --stat
```

If a workspace/diff tool is available (for example Conductor's `mcp__conductor__GetWorkspaceDiff`), prefer it, since it reflects exactly what the PR will contain.

## Step 5: Open the pull request

* Search for a PR template first: `.github/pull_request_template.md`, `.github/PULL_REQUEST_TEMPLATE.md`, or files under `.github/PULL_REQUEST_TEMPLATE/`. If one exists, structure the description around it.
* Create the PR with the GitHub CLI. Pass `--base <target-branch>` when you resolved an explicit target in Step 1; omit `--base` to let `gh` default to the base repo's default branch:

```shell
gh pr create --base <target-branch> --title "<title>" --body "<description>"
```

Constraints for the PR content:

* **Title** under 80 characters.
* **Description** under five sentences unless the user asked for more (or a template requires more).
* Describe **all** changes on the branch versus the base (from Step 4), not only what changed in the current session.

If `gh` is unavailable or not authenticated, print the branch, base, and a ready-to-paste title/description and give the user the compare URL instead.

## On failure

If any step fails, stop and ask the user how to proceed rather than forcing it. Report what succeeded (committed? pushed?) so they know the current state.

## Report back

When done, give the user the PR URL (from `gh`'s output) and a one-line summary of what was committed and pushed.

If you renamed the branch in Step 2.5, say so explicitly — `brave-falcon` →
`feat/session-card-labels` — since the user's tmux session and worktree directory
still carry the old name.
