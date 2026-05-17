---
name: hivemind-github-swarm-loop
description: Maintain Hivemind's multi-loop GitHub swarm automation. Use when changing `.agents/swarm.sh`, `.agents/swarm-launchd.sh`, `.agents/loop-common.sh`, `.agents/role-loop.sh`, `.agents/scout-loop.sh`, `.agents/worker-loop-a.sh`, `.agents/worker-loop-b.sh`, `.agents/pr-shepherd.sh`, `.agents/SWARM.md`, or the `PROMPT-*.md` loop prompts.
---

# Hivemind GitHub Swarm Loop

## Purpose

Keep the repository's multi-loop GitHub automation aligned around dedicated worktrees, deterministic issue ownership, and a separate PR shepherd role.

## Required Behavior

- Every loop requires working `gh` auth and issue access before it starts.
- Every loop must run Codex with GitHub-capable network access.
- Startup sections in the role prompts should stay brief and nix-first. `flake.nix` is the main toolchain source; `.agents/TOOLS.md` is for external or host-managed exceptions.
- `scout` audits the repo and opens only missing high-signal issues.
- `scout` is the only loop allowed to use the Codex browser tool, and only on the default branch to validate shipped behavior and file concrete new issues.
- `worker-a` owns odd-numbered issues.
- `worker-b` owns even-numbered issues.
- Development workers use one issue branch at a time in their own dedicated worktrees.
- Workers, PR shepherd, feature-request drafting, and general development lanes must not use the Codex browser tool.
- Every loop should prepend the shared subagent prompt and use bounded subagents for reconnaissance or disjoint sidecar work when delegation is available.
- The swarm should support an endless supervisor mode so opening the laptop can resume or keep running the improvement loops without a manual terminal babysitter.
- Workers open or update PRs but do not merge them.
- `pr-shepherd` merges ready PRs and fixes obvious CI failures only when it is not stealing an active worker branch.
- After a branch's PR merges, closes, or is canceled, the owning worktree should clean up that branch and return to the default-branch base before taking new work.

## Layout

- Shared helpers live in `.agents/loop-common.sh` and `.agents/role-loop.sh`.
- Role wrappers live in `.agents/scout-loop.sh`, `.agents/worker-loop-a.sh`, `.agents/worker-loop-b.sh`, and `.agents/pr-shepherd.sh`.
- Shared subagent policy lives in `.agents/PROMPT-subagents.md`.
- Role prompts live in `.agents/PROMPT-scout.md`, `.agents/PROMPT-worker-a.md`, `.agents/PROMPT-worker-b.md`, and `.agents/PROMPT-pr-shepherd.md`.
- The launcher and monitor entrypoint is `.agents/swarm.sh`.
- Optional laptop-start automation lives in `.agents/swarm-launchd.sh`.
- Repo-local runtime state belongs under `.agents/runtime/` and must stay gitignored.

## Verification

Run focused verification after changes:

```bash
bash -n .agents/loop-common.sh
bash -n .agents/role-loop.sh
bash -n .agents/scout-loop.sh
bash -n .agents/worker-loop-a.sh
bash -n .agents/worker-loop-b.sh
bash -n .agents/pr-shepherd.sh
bash -n .agents/swarm.sh
bash -n .agents/swarm-launchd.sh
bash -n .agents/verify-swarm.sh
bash .agents/verify-swarm.sh
```

Also run `qlty check` on every changed loop or skill file.
