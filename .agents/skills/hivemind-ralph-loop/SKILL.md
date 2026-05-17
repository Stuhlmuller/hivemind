---
name: hivemind-ralph-loop
description: Maintain Ralph's GitHub-driven agent loop. Use when changing `.agents/ralph.sh`, `.agents/PROMPT.md`, or Ralph rules around GitHub auth, issue selection, issue branches, PR flow, merge flow, network access, or loop enforcement.
---

# Hivemind Ralph Loop

## Purpose

Keep Ralph opinionated and enforceable. Ralph is not a generic local coding loop. It is a GitHub-driven workflow that must read issues, work one issue per branch, open a PR, and merge or stop.

## Required Behavior

- `gh` must be available and authenticated before Ralph starts.
- Ralph must be able to read repository issues before spawning Codex.
- Ralph must run Codex with network-capable access so nested `gh` commands can reach GitHub.
- GitHub failures are blockers. Do not add local-only fallback behavior.
- Ralph must use issue branches named `issue-<number>-<slug>`.
- Ralph issue execution must happen inside dedicated git worktrees.
- Ralph must reject runs that check out an issue branch locally in the active checkout before creating a worktree.
- Ralph must reject repurposing an existing issue worktree by checking out a different issue branch in place.
- A run that never creates or enters an issue worktree must fail.

## Wrapper Rules

1. Keep GitHub preflight checks in `.agents/ralph.sh`.
2. Keep branch enforcement in the wrapper, not only in the prompt.
3. Require a fresh worktree for each new issue branch. Do not accept in-place issue checkouts, even if the run later moves work into a worktree.
4. If the wrapper cannot verify the branch rule, exit non-zero before continuing the loop.
5. Keep the prompt and wrapper aligned whenever you change either one.

## Prompt Rules

- `.agents/PROMPT.md` must explicitly say that GitHub CLI is required.
- `.agents/PROMPT.md` must explicitly say that Ralph fails when required `gh` commands fail.
- `.agents/PROMPT.md` must explicitly say that worktree-only issue execution and local checkout activity are audited by the wrapper.

## Verification

Run focused verification after Ralph changes:

```bash
bash -n .agents/ralph.sh
```

Also run at least one stubbed loop check that proves:

- Ralph launches Codex with network-capable access.
- Ralph accepts a run that creates an `issue-<number>-<slug>` worktree directly.
- Ralph fails when a run checks out an `issue-<number>-<slug>` branch locally before creating a worktree.
- Ralph fails when a run repurposes an existing issue worktree by checking out a different issue branch in place.
