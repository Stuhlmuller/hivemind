---
name: hivemind-signed-commits
description: Require signed Git commits for Hivemind changes. Use before creating, amending, or pushing commits so Codex does not disable signing and fixes any unsigned commit before updating a branch or PR.
---

# Hivemind Signed Commits

## Rule

All Codex-authored commits for this repository must be signed.

## Workflow

1. Before committing, confirm signing is enabled:
   - `git config --get commit.gpgsign`
2. Do not disable signing with `-c commit.gpgsign=false` or similar overrides.
3. If a commit was created unsigned, repair it before leaving the branch in place:
   - `git commit --amend --no-edit -S`
4. If that unsigned commit was already pushed, update the remote branch with:
   - `git push --force-with-lease origin $(git branch --show-current)`

## Verification

- Verify the latest commit with:
  - `git log -1 --show-signature --oneline`
