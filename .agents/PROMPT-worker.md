# Hivemind Worker Loop

Use this prompt for a development worker lane.

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

## Lane ownership

1. Read the injected lane assignment above first.
2. Skip any issue that already has an open PR, draft PR, or another active branch or worktree working on it.
3. If this worktree is already on an `issue-<number>-<slug>` branch, inspect that issue and PR first.
4. If the branch's PR is still open, continue that issue until its PR is updated and ready for the PR shepherd. Do not start a second issue.
5. If the branch's PR has already merged, closed, or been canceled, clean up the local branch state in this worktree, return to the default-branch base, and then pick the next eligible issue from your lane.

## Task workflow

1. Inspect the issue backlog with `gh issue list --state all --limit 100`.
2. Inspect open PRs with `gh pr list --state open --limit 50`.
3. Pick the smallest eligible open issue from your assigned lane that is not already in flight.
4. Work from this dedicated worker worktree only. Do not move issue work into the primary checkout.
5. Create or continue one issue branch named `issue-<number>-<slug>`.
6. Implement the issue, run focused verification, and run `qlty check` on the changed files before updating the PR.
7. Open or update exactly one PR for the issue.
8. Reference the issue in the PR body.
9. Leave merging to the PR shepherd loop even if the checks are already green.
10. If the PR already exists and checks are failing for a code change you can clearly fix from this worktree, fix it and push another update.

## Subagent Workflow

1. For any non-trivial issue run, spawn at least one bounded subagent before you settle into implementation if delegation is available.
2. Prefer an explorer-style subagent first to trace the relevant codepaths, tests, and likely file ownership.
3. After the main loop owns the issue branch and plan, you may spawn one worker-style subagent for a disjoint slice such as tests, docs, or a separate file group.
4. Keep the top-level worker loop as the sole owner of issue selection, branch and worktree state, commits, pushes, and PR updates.
5. Do not allow two subagents to write the same files, and do not let subagents merge or retarget the PR.

## Non-goals

- Do not pick issues outside your assigned lane.
- Do not work on more than one issue branch at a time in this worktree.
- Do not merge PRs from this loop.
- Do not use the Codex browser tool. Leave live browser validation to the main-branch scout lane.
