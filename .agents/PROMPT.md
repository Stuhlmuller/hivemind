# Agent Bootstrap Prompt

Use this prompt at the start of every new agent spawn for this repository.

## Startup requirements

Before doing any other work:

1. Ensure `flake.nix` exists and includes every CLI you plan to use in the current run.
2. Ensure `.agents/TOOLS.md` exists and lists every CLI used in the current run, its nix package name, and why it is needed.
3. If you introduce a new CLI during the run, update `flake.nix` immediately. If the flake cannot be made usable in the current environment, record the tool in `.agents/TOOLS.md` before continuing.
4. Prefer working from the nix shell when available so the toolchain is consistent across agent spawns.

## Issue workflow

1. Inspect the repository issues with the GitHub CLI before proposing work:
   - Run `gh issue list --state all --limit 100`
2. If issues already exist:
   - Read them first.
   - Avoid creating duplicates.
   - Use the existing issues to guide prioritization.
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

## PR workflow

1. Each pull request must cover exactly one task.
2. Do not bundle multiple unrelated fixes or features into the same PR.
3. If a task needs separate follow-up work, open separate issues and separate PRs.
4. After opening a PR, check its status with GitHub CLI.
5. If checks are passing, merge the PR.
6. Do not mark an issue completed until its associated PR is merged.
7. If the PR is abandoned, closed, or canceled, update the issue state accordingly instead of marking it completed.

## Quality bar

- Be critical, not cosmetic.
- Prefer issues around reliability, testing, security, error handling, release safety, observability, performance, onboarding, documentation, and UX gaps when the codebase justifies them.
- Do not file vague tickets.
- Keep `flake.nix` and `.agents/TOOLS.md` in sync for every run.
