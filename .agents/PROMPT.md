# Agent Bootstrap Prompt

Use this prompt at the start of every new agent spawn for this repository.

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
5. Ralph runs with full access specifically so `gh` can reach GitHub; treat unexpected GitHub network failures as blockers, not soft warnings.
6. Do not skip issue or PR automation, and do not continue with a local-only fallback when GitHub CLI is broken.

## Issue workflow

1. Inspect the repository issues before proposing work:
   - Run `gh issue list --state all --limit 100`
2. If issues already exist:
   - Read them first.
   - Avoid creating duplicates.
   - Use the existing issues to guide prioritization.
   - Pick up exactly one issue at a time.
3. If the repository has no issues:
   - Audit the project critically.
   - Create exactly 8 issues for application improvements.
   - Create exactly 2 issues for new feature requests.
   - Use `gh issue create` for all 10 issues.
   - Each issue must have a concrete title and body covering:
     - the problem or opportunity
     - why it matters
     - the expected outcome
     - any evidence from the codebase

## Task branch workflow

1. Every issue picked up for implementation must get its own fresh branch.
2. When starting new issue work from the default branch, create the issue branch in its own fresh git worktree before making code changes. Use `git worktree add -b issue-<number>-<slug> <path> <default-branch>` or equivalent instead of checking out the issue branch in place first.
3. Use one branch for one issue only.
4. Name branches from the issue number and task, for example `issue-123-short-slug`.
5. Ralph only uses dedicated git worktrees for issue execution. The primary checkout must stay off issue branches.
6. Do all implementation, validation, commits, and PR work inside that issue worktree.
7. Never repurpose an existing Ralph worktree by checking out a different issue branch in place. Create a fresh worktree for every new issue.
8. Do not start work on another issue until the current issue branch has been turned into a PR and that PR is merged, closed, or canceled.
9. Ralph audits branch naming, checkout activity, and worktree isolation. If a run creates or continues an `issue-<number>-<slug>` branch via local checkout instead of a dedicated worktree, the wrapper will fail the run.

## PR workflow

1. Each pull request must cover exactly one task.
2. Do not bundle multiple unrelated fixes or features into the same PR.
3. Create a PR for every issue branch that Ralph picks up from the issue list.
4. Reference the issue in the PR body so the relationship is explicit.
5. If a task needs separate follow-up work, open separate issues and separate PRs.
6. After opening a PR, check its status with GitHub CLI.
7. If checks are passing, merge the PR.
8. Do not mark an issue completed until its associated PR is merged.
9. If the PR is abandoned, closed, or canceled, update the issue state accordingly instead of marking it completed.

## Quality bar

- Be critical, not cosmetic.
- Prefer issues around reliability, testing, security, error handling, release safety, observability, performance, onboarding, documentation, and UX gaps when the codebase justifies them.
- Do not file vague tickets.
- Keep `flake.nix` and `.agents/TOOLS.md` in sync for every run.
