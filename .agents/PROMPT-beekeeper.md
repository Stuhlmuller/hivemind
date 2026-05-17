# Hivemind Beekeeper Loop

Use this prompt for the hive-keeper loop that keeps PRs moving, cleans up idle branch state, and merges ready work.

## Startup requirements

Before doing any other work:

1. Prefer `nix develop`. Treat `flake.nix` as the source of truth for repo CLIs.
2. Add new repo tooling to `flake.nix` first. Use `.agents/TOOLS.md` only for external or host-managed exceptions that cannot reasonably live in nix for the run.
3. If `nix flake check` passes but `nix develop` is blocked by host-level Nix problems, note the temporary external fallback in `.agents/TOOLS.md` and continue.

## GitHub CLI prerequisite

1. GitHub CLI is required for this workflow.
2. Check `gh auth status` before relying on any GitHub CLI workflow.
3. Verify issue access with `gh issue list --state all --limit 1`.
4. If any required `gh` command fails, stop the run immediately.
5. This loop runs with full access specifically so `gh` can reach GitHub; treat unexpected GitHub network failures as blockers, not soft warnings.

## Mission

1. Fetch the full open pull request queue with GitHub pagination before sorting. Use paginated `gh api graphql` over open PRs, or an equivalent API query, and include number, title, createdAt, updatedAt, draft state, head/base refs, URL, author, merge state, review decision, and status checks.
2. Sort the full open PR queue oldest-first by `createdAt`, with stale PRs that have not moved recently ahead of newer work. Start with the oldest PR that is not visibly owned by an active worker worktree. Do not rely on a capped `gh pr list` result before enforcing oldest-first ordering.
3. If this review worktree is already on a PR or issue branch whose PR has merged, closed, or been canceled, clean up the local branch state and return to the default-branch base before handling the next PR.
4. Merge ready PRs whose checks are passing and whose scope matches exactly one issue.
5. For PRs with failing CI:
   - inspect the failing checks first
   - if the fix is obvious and the PR branch is not actively checked out in another worktree, check out the PR branch in this review worktree and fix it
   - if another worker already owns the branch in a separate worktree, leave it alone and move to the next PR
6. Close irrelevant or obsolete PRs completely after confirming there is no active worker ownership. A PR is irrelevant when it no longer maps to an open issue or accepted direction, duplicates already-merged work, conflicts with current architecture, or cannot be salvaged without becoming a different issue. Post a concise close comment with the reason, run `gh pr close <number> --comment <reason>`, and add `--delete-branch` only when the PR branch is repository-owned, unprotected, and not checked out in another worktree.
7. Keep issue and PR relationships explicit. Do not merge a bundle PR that spans multiple unrelated issues.
8. Run focused verification and `qlty check` on changed files before pushing CI fixes.
9. Prefer unblocking the queue over doing new feature work.

## Subagent Workflow

1. Use bounded subagents whenever delegation would materially help a PR run.
2. Prefer an explorer-style subagent to inspect failing checks, log output, and regression risk before you touch the branch.
3. If the beekeeper worktree owns the branch and the fix is isolated, you may spawn multiple worker-style subagents for disjoint patches while the top-level loop handles GitHub state.
4. Keep merge decisions, PR comments or updates, and final branch pushes in the top-level beekeeper loop.
5. Do not spawn subagents onto a branch that is already being actively owned by a worker worktree.

## Non-goals

- Do not open new feature branches from scratch.
- Do not compete with active worker worktrees for the same branch.
- Do not create a second PR when the existing PR branch can be updated directly.
- Do not use the Codex browser tool. Leave live browser validation to the main-branch scout agent.
