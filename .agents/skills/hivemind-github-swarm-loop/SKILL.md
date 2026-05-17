---
name: hivemind-github-swarm-loop
description: Maintain Hivemind's GitHub swarm automation. Use when changing `.agents/swarm.sh`, `.agents/swarm-roles.sh`, `.agents/agent-loop.sh`, `.agents/swarm-launchd.sh`, `.agents/loop-common.sh`, `.agents/role-loop.sh`, `.agents/scout-loop.sh`, `.agents/reviewer-loop.sh`, `.agents/worker-loop.sh`, `.agents/feature-requester-loop.sh`, `.agents/beekeeper-loop.sh`, `.agents/browser-user-loop.sh`, `.agents/developer-loop.sh`, `.agents/worker-loop-a.sh`, `.agents/worker-loop-b.sh`, `.agents/pr-shepherd.sh`, `.agents/SWARM.md`, or the `PROMPT-*.md` loop prompts.
---

# Hivemind GitHub Swarm Loop

## Purpose

Keep the repository's GitHub swarm automation aligned around dedicated
worktrees, canonical top-level roles, compatibility aliases, bounded
delegation, and safe PR handling.

## Required Behavior

- Every loop requires working `gh` auth and issue access before it starts.
- Every loop must run Codex with GitHub-capable network access.
- Startup sections in the role prompts should stay brief and nix-first. `flake.nix` is the main toolchain source; `.agents/TOOLS.md` is for external or host-managed exceptions.
- The official top-level roles are `reviewer`, `feature-requester`, `worker`, `scout`, and `beekeeper`.
- `scout` is the only loop allowed to use the Codex browser tool, and only on the default branch to validate shipped behavior and file concrete new issues.
- `reviewer` audits the repo, open PRs, tests, docs, and release posture, then opens grounded issue follow-ups without doing implementation.
- `feature-requester` opens concrete feature backlog issues without using the browser tool or starting implementation.
- `worker` owns one issue branch at a time in its own dedicated worktree.
- `beekeeper` owns the PR queue, merges ready PRs, and fixes obvious CI failures only when it is not stealing an active worker branch.
- Compatibility wrappers and aliases such as `browser-user`, `developer`, `worker-a`, `worker-b`, and `pr-shepherd` should keep routing into the canonical roles instead of reintroducing separate lane logic.
- Workers, beekeeper, feature-request drafting, and general development loops must not use the Codex browser tool.
- Every loop should prepend the shared subagent prompt and allow bounded subagents whenever delegation is available and the tasks are concrete and non-overlapping.
- The swarm should support an endless supervisor mode so opening the laptop can resume or keep running the improvement loops without a manual terminal babysitter.
- Workers open or update PRs but do not merge them.
- After a branch's PR merges, closes, or is canceled, the owning worktree should clean up that branch and return to the default-branch base before taking new work.

## Layout

- Shared helpers live in `.agents/loop-common.sh`, `.agents/swarm-roles.sh`, `.agents/agent-loop.sh`, and `.agents/role-loop.sh`.
- Canonical role wrappers live in `.agents/reviewer-loop.sh`, `.agents/feature-requester-loop.sh`, `.agents/worker-loop.sh`, `.agents/scout-loop.sh`, and `.agents/beekeeper-loop.sh`.
- Compatibility wrappers live in `.agents/browser-user-loop.sh`, `.agents/developer-loop.sh`, `.agents/worker-loop-a.sh`, `.agents/worker-loop-b.sh`, and `.agents/pr-shepherd.sh`.
- Shared subagent policy lives in `.agents/PROMPT-subagents.md`.
- Role prompts live in `.agents/PROMPT-scout.md`, `.agents/PROMPT-reviewer.md`, `.agents/PROMPT-worker.md`, `.agents/PROMPT-feature-requester.md`, and `.agents/PROMPT-beekeeper.md`.
- The launcher and monitor entrypoint is `.agents/swarm.sh`.
- Optional laptop-start automation lives in `.agents/swarm-launchd.sh`.
- Repo-local runtime state belongs under `.agents/runtime/` and must stay gitignored.

## Verification

Run focused verification after changes:

```bash
bash -n .agents/loop-common.sh
bash -n .agents/swarm-roles.sh
bash -n .agents/agent-loop.sh
bash -n .agents/role-loop.sh
bash -n .agents/reviewer-loop.sh
bash -n .agents/feature-requester-loop.sh
bash -n .agents/scout-loop.sh
bash -n .agents/worker-loop.sh
bash -n .agents/beekeeper-loop.sh
bash -n .agents/browser-user-loop.sh
bash -n .agents/developer-loop.sh
bash -n .agents/worker-loop-a.sh
bash -n .agents/worker-loop-b.sh
bash -n .agents/pr-shepherd.sh
bash -n .agents/swarm.sh
bash -n .agents/swarm-launchd.sh
bash -n .agents/verify-swarm.sh
bash .agents/verify-swarm.sh
```

Also run `qlty check` on every changed loop or skill file.
