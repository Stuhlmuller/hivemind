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
- Ralph should aggressively fan out bounded subagents inside the active issue run when delegation is available, while still keeping one issue, one branch, and one PR in flight.
- Reviewer lanes should scale with worker lanes, and feature-request drafting lanes should outnumber worker lanes whenever the backlog still has concrete gaps. QA and issue-finder lanes may run opportunistically.
- Recoverable `codex exec` and `codex review` failures should restart the loop with a recovery instruction instead of killing Ralph outright.
- Ralph must reject runs that check out an issue branch locally in the active checkout before creating a worktree.
- Ralph must reject repurposing an existing issue worktree by checking out a different issue branch in place.
- A run that never creates or enters an issue worktree must fail.

## Wrapper Rules

1. Keep GitHub preflight checks in `.agents/ralph.sh`.
2. Keep branch enforcement in the wrapper, not only in the prompt.
3. Require a fresh worktree for each new issue branch. Do not accept in-place issue checkouts, even if the run later moves work into a worktree.
4. If the wrapper cannot verify the branch rule, exit non-zero before continuing the loop.
5. Keep the Ralph-specific delegation prompt prepended by the wrapper, or otherwise guaranteed for every Ralph run.
6. Keep recovery prompt behavior in the wrapper and `.agents/PROMPT.md` aligned whenever you change either one.

## Prompt Rules

- `.agents/PROMPT.md` should stay terse and nix-first. Push routine toolchain detail into `flake.nix` instead of bloating the prompt.
- `.agents/TOOLS.md` should mainly record external or host-managed exceptions, not restate the dev shell contents.
- `.agents/PROMPT.md` must explicitly say that GitHub CLI is required.
- `.agents/PROMPT.md` must explicitly say that Ralph fails when required `gh` commands fail.
- `.agents/PROMPT.md` must explicitly say that worktree-only issue execution and local checkout activity are audited by the wrapper.
- `.agents/PROMPT.md` must explicitly say that Ralph stays on exactly one issue, branch, and PR even when it spawns many subagents.
- `.agents/PROMPT.md` must explicitly say to use as many parallel bounded subagents as the runtime safely supports for disjoint work on that one issue.
- `.agents/PROMPT.md` must explicitly encode the reviewer-to-worker and feature-requester-to-worker pacing rules, plus the looser QA and issue-finder pacing.
- `.agents/PROMPT.md` must explicitly allow backlog-drafting subagents to research future issues without starting implementation outside the current issue branch.
- `.agents/PROMPT.md` must explicitly reserve the Codex browser tool for main-branch issue scouting only.
- `.agents/PROMPT.md` must explicitly say that recovery instructions do not weaken GitHub or worktree blockers.

## Verification

Run focused verification after Ralph changes:

```bash
bash -n .agents/ralph.sh
```

Also run at least one stubbed loop check that proves:

- Ralph launches Codex with network-capable access.
- Ralph retries recoverable `codex exec` failures with a recovery instruction.
- Ralph retries recoverable `codex review` failures with a recovery instruction.
- Ralph prepends the bounded-subagent fanout policy to every Codex run.
- Ralph accepts a run that creates an `issue-<number>-<slug>` worktree directly.
- Ralph fails when a run checks out an `issue-<number>-<slug>` branch locally before creating a worktree.
- Ralph fails when a run repurposes an existing issue worktree by checking out a different issue branch in place.
