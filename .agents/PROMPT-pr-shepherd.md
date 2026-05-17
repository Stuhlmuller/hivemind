# Hivemind PR Shepherd Loop

Use this prompt for the CI triage, PR review, and merge loop.

## Startup requirements

Before doing any other work:

1. Ensure `flake.nix` exists and includes every CLI you plan to use in the current run.
2. Ensure `.agents/TOOLS.md` exists and lists every CLI used in the current run, its nix package name, and why it is needed.
3. If you introduce a new CLI during the run, update `flake.nix` immediately. If the flake cannot be made usable in the current environment, record the tool in `.agents/TOOLS.md` before continuing.
4. Prefer working from the nix shell when available so the toolchain is consistent across agent spawns.
5. If `nix flake check` passes but `nix develop` is blocked by host-level CA, daemon, or other machine-local Nix configuration, treat the dev shell as unavailable for that run. Record any needed external tools in `.agents/TOOLS.md` and continue instead of stalling on local environment repair.

## GitHub CLI prerequisite

1. GitHub CLI is required for this workflow.
2. Check `gh auth status` before relying on any GitHub CLI workflow.
3. Verify issue access with `gh issue list --state all --limit 1`.
4. If any required `gh` command fails, stop the run immediately.
5. This loop runs with full access specifically so `gh` can reach GitHub; treat unexpected GitHub network failures as blockers, not soft warnings.

## Mission

1. Inspect open pull requests with `gh pr list --state open --limit 50`.
2. If this review worktree is already on a PR or issue branch whose PR has merged, closed, or been canceled, clean up the local branch state and return to the default-branch base before handling the next PR.
3. Merge ready PRs whose checks are passing and whose scope matches exactly one issue.
4. For PRs with failing CI:
   - inspect the failing checks first
   - if the fix is obvious and the PR branch is not actively checked out in another worktree, check out the PR branch in this review worktree and fix it
   - if another worker already owns the branch in a separate worktree, leave it alone and move to the next PR
5. Keep issue and PR relationships explicit. Do not merge a bundle PR that spans multiple unrelated issues.
6. Run focused verification and `qlty check` on changed files before pushing CI fixes.
7. Prefer unblocking the queue over doing new feature work.

## Non-goals

- Do not open new feature branches from scratch.
- Do not compete with active worker worktrees for the same branch.
- Do not create a second PR when the existing PR branch can be updated directly.
