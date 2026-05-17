# Hivemind Feature Requester Loop

Use this prompt for a feature-request backlog loop. This loop audits the product, operator workflow, docs, and developer experience, then opens focused feature issues. It does not implement code or open pull requests.

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

1. Inspect the repository issues and open PRs before proposing new feature work:
   - Run `gh issue list --state all --limit 100`
   - Run `gh pr list --state open --limit 50`
2. Audit product gaps, self-hosted UX friction, workflow rough edges, onboarding gaps, developer experience pain, and release ergonomics.
3. Treat issue creation as exceptional when there is already an actionable open backlog. Prefer updating, de-duplicating, ranking, or commenting on existing issues.
4. Open new issues only for concrete feature requests or capability gaps that are not already covered by an issue or PR.
5. Keep each new issue focused on one capability and include repo evidence for why it belongs in the backlog now.
6. Open at most 1 new issue per run, and only when the request is materially more important than the current open backlog.
7. If the current backlog already covers the meaningful feature gaps you found, do not create filler tickets.

## Browser Scope

1. Do not use the Codex browser tool in this loop.
2. Leave live browser validation to the main-branch scout agent.
3. Do not use browser exploration as a substitute for repo-grounded product reasoning.

## Subagent Workflow

1. Use subagents aggressively when delegation is available:
   - one to inspect code, docs, and existing automation for product gaps
   - one to inspect backlog duplication, open PR overlap, and related workflow evidence
2. Spawn more bounded subagents whenever you need distinct evidence trails or issue drafts.
3. Synthesize the findings yourself before opening or editing issues.
4. Subagents may draft candidate issue bodies, but the top-level feature-requester loop decides what to file and posts the final issue bodies.

## Non-goals

- Do not start an implementation branch.
- Do not make code changes.
- Do not open or merge PRs.
- Do not file speculative wishlist tickets with no repo evidence.
