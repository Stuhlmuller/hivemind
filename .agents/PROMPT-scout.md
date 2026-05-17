# Hivemind Scout Loop

Use this prompt for the issue-scout loop. This loop audits the repository and backlog, then opens or refines GitHub issues. It does not implement code or open pull requests.

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

1. Inspect the repository issues before proposing work:
   - Run `gh issue list --state all --limit 100`
   - Run `gh pr list --state open --limit 50`
2. Audit the current codebase, tests, docs, automation scripts, and release gaps.
3. Open new issues only for gaps that are not already covered by existing issues or open PRs.
4. Prefer high-signal issues around security, reliability, tests, CI, release safety, observability, onboarding, and agent automation.
5. Keep each new issue focused on one concern.
6. Include concrete evidence from the codebase in every issue body.
7. Open at most 2 new issues per run unless the backlog is empty.
8. If the current issue set already covers the meaningful gaps you found, do not create filler tickets.

## Subagent Workflow

1. For every real scout run, spawn at least two bounded subagents if delegation is available:
   - one to audit code, tests, and security or reliability gaps
   - one to audit docs, automation, CI, and backlog duplication
2. Synthesize the findings yourself before opening or editing issues.
3. Subagents may gather evidence or draft issue candidates, but the top-level scout loop decides what to file and posts the final issue bodies.

## Non-goals

- Do not start an implementation branch.
- Do not make code changes.
- Do not open or merge PRs.
- Do not create vague cleanup tickets.
