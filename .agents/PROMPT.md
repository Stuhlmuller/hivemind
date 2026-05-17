# Agent Bootstrap Prompt

Use this prompt at the start of every new agent spawn for this repository.

## Startup requirements

Before doing any other work:

1. Prefer `nix develop`. Treat `flake.nix` as the source of truth for repo CLIs.
2. Add new repo tooling to `flake.nix` first. Use `.agents/TOOLS.md` only for external or host-managed exceptions that cannot reasonably live in nix for the run.
3. If `nix flake check` passes but `nix develop` is blocked by host-level Nix problems, note the temporary external fallback in `.agents/TOOLS.md` and continue.

## GitHub CLI prerequisite

1. `gh` is required. Run `gh auth status` and `gh issue list --state all --limit 1` before relying on GitHub automation.
2. Any required `gh` failure blocks the run. GitHub-driven repo work must not fall back to a local-only workflow.

## Issue workflow

1. Start with `gh issue list --state all --limit 100`.
2. Prefer existing issues and work exactly one issue at a time.
3. If the repository has no issues, create exactly 8 improvement issues and 2 feature-request issues with concrete problem, why, expected outcome, and codebase evidence.

## Task branch workflow

1. Use one issue, one fresh `issue-<number>-<slug>` branch, and one dedicated git worktree.
2. Create the branch with `git worktree add -b ...` from the default branch. Never check out the issue branch in place in the primary checkout.
3. Do all implementation, validation, commits, and PR work inside that worktree.
4. Never repurpose an issue worktree for a different issue.
5. Do not start another issue until the current one is merged, closed, or canceled.

## PR workflow

1. Keep PR scope to exactly one issue and reference that issue in the PR body.
2. Open or update one PR per issue and inspect PR status with `gh`.
3. Merge only when checks pass.
4. If follow-up work is needed, open separate issues and PRs.
5. Do not mark an issue complete until its PR is merged. If the PR is abandoned, closed, or canceled, update the issue state accordingly.

## Subagent workflow

1. After selecting one issue and moving into its dedicated worktree, delegate aggressively if available.
2. Use as many concurrent bounded subagents as safe for concrete disjoint tasks on that issue.
3. Keep reviewer coverage at least even with worker coverage, and keep future-feature-request drafting lanes ahead of worker coverage while real backlog gaps remain. QA tester and issue-finder lanes can be opportunistic.
4. Prefer explorer subagents for reconnaissance, risk review, test gaps, and feature-request drafting. Prefer worker subagents only for explicit disjoint write scopes.
5. The top-level run owns issue selection, worktree and branch state, final validation, commits, pushes, PR actions, merge decisions, and final decisions about any drafted backlog issues.
6. No subagent may create another implementation branch or PR or start coding a second issue. Backlog drafting is allowed; implementation stays on the current issue branch.

## Browser tool scope

1. Reserve the Codex browser tool for main-branch issue scouting that validates shipped behavior and files concrete new issues.
2. Do not use it for feature-request drafting, implementation, PR updates, or general development.

## Recovery workflow

1. If the current thread includes a `Recovery Instruction`, treat it as the top-priority blocker.
2. Fix that blocker first, then restart the normal issue-driven flow from the top in the same run.
3. Recovery instructions do not weaken GitHub or worktree blockers.

## Quality bar

- Be critical, not cosmetic.
- Prefer issues grounded in reliability, testing, security, error handling, release safety, observability, performance, onboarding, documentation, or UX gaps.
- Do not file vague tickets.
- Keep `flake.nix` and `.agents/TOOLS.md` aligned for the run.
