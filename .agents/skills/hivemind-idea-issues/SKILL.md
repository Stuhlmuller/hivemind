---
name: hivemind-idea-issues
description: Brainstorm and refine new Hivemind product, security, UX, operations, and developer-experience ideas, then turn the best ideas into labeled GitHub issues instead of implementation work. Use when the user asks for new ideas, roadmap or backlog suggestions, gap analysis, what to build next, or wants opportunities captured as issues without starting development.
---

# Hivemind Idea Issues

## Goal

Turn Hivemind idea generation into careful backlog capture. Bias toward refining or ranking existing issues when the backlog already has actionable work. Create issues only. Do not write product code, create branches, open PRs, or start implementation unless the user explicitly changes scope.

## Workflow

1. Inspect the repo surfaces that match the user's prompt before inventing ideas.
2. Widen the audit just enough to find the best candidate without generating a pile of speculative backlog. Check adjacent product, security, reliability, operations, docs, tests, release, and developer-experience gaps only when the user's prompt needs that breadth.
3. Verify GitHub access with `gh auth status`.
4. Read the current backlog with `gh issue list --state all --limit 100`.
5. Read the repository label taxonomy with `gh label list --limit 100`.
6. Reject duplicates by comparing candidate ideas against open and closed issues.
7. Ground each proposed idea in a concrete repo gap, risk, inconsistency, or missing capability.
8. Classify every accepted issue candidate before filing or editing it.
9. Prefer refining, de-duplicating, ranking, or commenting on existing issues when they already cover the best ideas.
10. For broad idea or gap-analysis prompts, create at most one GitHub issue for the strongest accepted idea unless the user explicitly asks for a larger batch.
11. Stop after issue creation, or after deciding that existing issues are enough, and report the issue numbers or existing issue references with their labels.

## Issue Bar

- Keep each issue focused on one problem or opportunity.
- Cover the problem or opportunity, why it matters, the expected outcome, and evidence from the repo.
- Prefer security, reliability, release readiness, operator visibility, credential safety, self-hosted UX, agent coordination, and developer-experience gaps when the codebase supports them.
- If another grounded, non-duplicate idea exists but the backlog is already actionable, mention it as a candidate instead of automatically filing it.
- Reject vague "improve X" tickets, duplicate requests, and implementation detail that belongs in a PR instead of an issue.
- When the repo has no issues and the user asks for a fresh audit, follow `.agents/PROMPT.md`: create exactly 3 improvement issues and 1 feature-request issue.

## Label Discipline

- Never create an unlabeled issue. Use `gh issue create --label <label>` for new issues, and use `gh issue edit <number> --add-label <label>` when refining an existing issue that lacks the right classification.
- Choose at least one primary classification label from the labels available in the repository.
- Ensure the reusable `security` label exists before filing security findings. If it is missing, create it with `gh label create security --description "Security hardening, credential safety, auth, policy, or secret-handling work" --color d73a4a`.
- Use `security` for credential separation, secret handling, sessions, auth, policy enforcement, JIT leases, audit integrity, dependency exposure, browser hardening, or deployment hardening.
- Use `bug` for broken current behavior, regressions, incorrect state, failed expected workflows, data loss risks caused by existing behavior, or checks that should already pass.
- Use `enhancement` for new capabilities, operator workflow improvements, planned UX additions, automation features, and developer-experience improvements.
- Prefer a more specific existing label, such as `documentation`, `testing`, `ci`, `release`, `ops`, `frontend`, `agent-runtime`, or `credential-safety`, when that label exists and better describes the issue. Keep the broad primary label too when it adds useful triage context.
- If no exact label exists, use the nearest broad label. Create a new short, lowercase label only when the category is likely to be reused; otherwise mention the missing taxonomy in the final handoff instead of filing an unlabeled issue.

## Stop Conditions

- Stop and report a blocker if `gh auth status`, `gh issue list`, or `gh label list` fails.
- Stop and ask for a scope change if the user switches from backlog creation to implementation.
- Stop without filing when existing open issues already cover the meaningful candidates or when new candidates would be lower value than the current backlog.
