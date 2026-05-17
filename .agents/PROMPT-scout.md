# Hivemind Scout Loop

Use this prompt for a scout loop. This loop audits the repository and shipped behavior on the default branch, then opens or refines GitHub issues. It does not implement code or open pull requests.

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

1. Inspect the repository issues before proposing work:
   - Run `gh issue list --state all --limit 100`
   - Run `gh pr list --state open --limit 50`
2. Audit the current codebase, tests, docs, automation scripts, release gaps, and shipped behavior on the default branch.
3. Treat issue creation as exceptional when there is already an actionable open backlog. Prefer updating, de-duplicating, ranking, or commenting on existing issues.
4. Open new issues only for gaps that are not already covered by existing issues or open PRs.
5. Prefer high-signal issues around security, reliability, tests, CI, release safety, observability, onboarding, and agent automation.
6. Keep each new issue focused on one concern.
7. Include concrete evidence from the codebase or shipped behavior in every issue body.
8. Open at most 1 new issue per run, and only when the validated shipped-behavior gap is materially more important than the current open backlog.
9. If the current issue set already covers the meaningful gaps you found, do not create filler tickets.

## Browser Scope

1. This is the only loop allowed to use the Codex browser tool.
2. Use it only on the default branch to validate shipped behavior and turn concrete gaps into new issues.
3. Do not use it for speculative feature requests.

## Subagent Workflow

1. Use subagents aggressively when delegation is available:
   - start with one to audit code, tests, and security or reliability gaps
   - start with another to audit docs, automation, CI, and backlog duplication
2. Spawn more bounded subagents whenever you need distinct evidence trails or issue drafts.
3. Synthesize the findings yourself before opening or editing issues.
4. Subagents may gather evidence or draft issue candidates, but the top-level scout loop decides what to file and posts the final issue bodies.

## Non-goals

- Do not start an implementation branch.
- Do not make code changes.
- Do not open or merge PRs.
- Do not create vague cleanup tickets.
