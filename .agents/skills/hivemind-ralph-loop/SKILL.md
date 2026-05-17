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
- A run that never switches onto an issue branch must fail.

## Wrapper Rules

1. Keep GitHub preflight checks in `.agents/ralph.sh`.
2. Keep branch enforcement in the wrapper, not only in the prompt.
3. Allow Ralph to prove branch creation either by ending on an issue branch or by showing issue-branch checkout activity during the run.
4. If the wrapper cannot verify the branch rule, exit non-zero before continuing the loop.
5. Keep the prompt and wrapper aligned whenever you change either one.

## Prompt Rules

- `.agents/PROMPT.md` must explicitly say that GitHub CLI is required.
- `.agents/PROMPT.md` must explicitly say that Ralph fails when required `gh` commands fail.
- `.agents/PROMPT.md` must explicitly say that branch creation is audited by the wrapper.

## Verification

Run focused verification after Ralph changes:

```bash
bash -n .agents/ralph.sh
```

Also run at least one stubbed loop check that proves:

- Ralph launches Codex with network-capable access.
- Ralph fails when no issue branch is opened.
- Ralph accepts a run that checks out an `issue-<number>-<slug>` branch.
