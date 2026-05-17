---
name: hivemind-branch-checkpoints
description: Keep Hivemind branch work moving with small signed checkpoint commits. Use when doing multi-step implementation on a branch, when several meaningful edits have piled up, or when you need to preserve progress before a risky change, context switch, or handoff.
---

# Hivemind Branch Checkpoints

## Rule

On any non-trivial Hivemind branch, make small signed checkpoint commits
throughout the work instead of waiting until the end. Each branch should keep a
reviewable trail of meaningful progress.

## When To Checkpoint

Create a checkpoint after:

- A feature slice works end to end.
- A security boundary changes.
- A refactor settles into a coherent state.
- Tests or verification pass for a meaningful milestone.
- Before a risky edit, context switch, or handoff.
- Before push or PR prep if the branch still has meaningful uncommitted work.

Do not checkpoint:

- Broken experiments you do not intend to keep.
- Unrelated files from outside the branch scope.
- Generated caches, local databases, virtualenvs, or secrets.
- Unsigned commits.

## Workflow

1. Confirm the branch still maps to one issue or one intentional task.
2. Review `git status --short` and keep the checkpoint scoped.
3. Run the smallest relevant verification for the milestone you are preserving.
4. Create a signed commit with a message that says what is now true.
5. Continue working and repeat after the next meaningful slice.

## Commit Shape

- Prefer multiple small commits over one large end-of-branch dump.
- Keep each commit readable enough to review, revert, or bisect independently.
- Use messages like `Add lease denial audit fields`, `Checkpoint auth migration guardrails`, or `Refactor credential store validation`.
- If the branch has moved through more than one meaningful change without a commit, stop and checkpoint before continuing.

## Relationship To Other Skills

- Use `hivemind-git-commits` when staging, committing, verifying signatures, pushing, or preparing branch history for PR work.
- Use this skill earlier in the implementation loop to decide when to checkpoint.

## Verification

Use:

```bash
git status --short
git log --show-signature -1
```
