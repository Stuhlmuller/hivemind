---
name: hivemind-git-commits
description: Handle Hivemind staging, commit, and push workflows. Use when preparing a checkpoint commit, cleaning up a branch before push, or verifying git hygiene for Hivemind changes so commits stay signed, scoped, and policy-compliant.
---

# Hivemind Git Commits

## Rule

Every Hivemind commit must be cryptographically signed. Do not create, amend, or
push unsigned commits.

## Workflow

1. Confirm the branch scope matches one issue or one intentional checkpoint.
2. Review `git status --short` so unrelated files do not get swept into the commit.
3. Run the narrowest meaningful verification for the touched files.
4. Create a signed commit. If signing fails, stop and fix signing instead of using an unsigned fallback.
5. Before push or PR creation, verify the new commit is signed and the worktree is in the expected state.

## Guardrails

- Do not disable commit signing for repository commits.
- Do not use unsigned local-only checkpoints as a workaround for policy checks.
- If the environment blocks signing, surface that as a blocker immediately.
- Keep one issue per branch and one task per PR.

## Verification

Use the smallest relevant checks for the change, then verify the commit state with:

```bash
git log --show-signature -1
git status --short
```
