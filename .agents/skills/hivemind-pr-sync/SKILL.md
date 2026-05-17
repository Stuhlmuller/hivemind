---
name: hivemind-pr-sync
description: Keep Hivemind branches current with `main` before PR handoff. Use when opening, refreshing, or asking for review on a PR, or before push when the branch may have drifted; fetch the latest `origin/main`, integrate it, resolve conflicts locally, rerun focused checks, and only then push or update the PR.
---

# Hivemind PR Sync

## Rule

Before opening, updating, or asking for review on a Hivemind pull request,
make sure the branch already contains the latest `origin/main`. Never hand off
a branch with unresolved conflicts or known drift from `main`.

## Workflow

1. Confirm the branch still maps to one issue or one intentional task and
   inspect `git status --short`.
2. Fetch the latest default branch state with `git fetch origin main`.
3. Reconcile the current branch with `origin/main` before PR handoff.
   - Prefer `git rebase origin/main` when linear history is appropriate.
   - Use a merge only when the branch already depends on merge commits or the
     user/repo workflow explicitly requires it.
4. If conflicts appear, resolve them immediately in the branch. Inspect the
   overlap carefully instead of choosing a side blindly.
5. Rerun the smallest relevant verification for the files affected by the sync.
6. Re-check signature and branch state after the sync, especially if a rebase
   rewrote commits.
7. Push and open or refresh the PR only after the branch is clean and verified.

## Conflict Policy

- Do not open or refresh a PR with conflict markers, unmerged paths, or a
  branch that does not contain the latest `origin/main`.
- Do not ask reviewers, maintainers, or the merge queue to resolve obvious
  branch conflicts for you.
- If conflict resolution changes behavior or creates doubt, stop and run more
  focused verification before continuing.

## Relationship To Other Skills

- Use `hivemind-git-commits` before or after this workflow when you need signed
  commit verification, staging hygiene, or push checks.
- Use `yeet` only when the user explicitly asks for the full
  stage/commit/push/PR flow. This skill still applies before the PR step.

## Verification

Use:

```bash
git fetch origin main
git merge-base --is-ancestor origin/main HEAD
git diff --name-only --diff-filter=U
git log --show-signature -1
git status --short
```
