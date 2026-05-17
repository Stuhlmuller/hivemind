---
name: hivemind-idea-issues
description: Brainstorm and refine new Hivemind product, security, UX, operations, and developer-experience ideas, then turn the best ideas into concrete GitHub issues instead of implementation work. Use when the user asks for new ideas, roadmap or backlog suggestions, gap analysis, what to build next, or wants opportunities captured as issues without starting development.
---

# Hivemind Idea Issues

## Goal

Turn Hivemind idea generation into careful backlog capture. Bias toward refining or ranking existing issues when the backlog already has actionable work. Create issues only. Do not write product code, create branches, open PRs, or start implementation unless the user explicitly changes scope.

## Workflow

1. Inspect the repo surfaces that match the user's prompt before inventing ideas.
2. Widen the audit just enough to find the best candidate without generating a pile of speculative backlog. Check adjacent product, security, reliability, operations, docs, tests, release, and developer-experience gaps only when the user's prompt needs that breadth.
3. Verify GitHub access with `gh auth status`.
4. Read the current backlog with `gh issue list --state all --limit 100`.
5. Reject duplicates by comparing candidate ideas against open and closed issues.
6. Ground each proposed idea in a concrete repo gap, risk, inconsistency, or missing capability.
7. Prefer refining, de-duplicating, ranking, or commenting on existing issues when they already cover the best ideas.
8. For broad idea or gap-analysis prompts, create at most one GitHub issue for the strongest accepted idea unless the user explicitly asks for a larger batch.
9. Stop after issue creation, or after deciding that existing issues are enough, and report the issue numbers or existing issue references.

## Issue Bar

- Keep each issue focused on one problem or opportunity.
- Cover the problem or opportunity, why it matters, the expected outcome, and evidence from the repo.
- Prefer security, reliability, release readiness, operator visibility, credential safety, self-hosted UX, agent coordination, and developer-experience gaps when the codebase supports them.
- If another grounded, non-duplicate idea exists but the backlog is already actionable, mention it as a candidate instead of automatically filing it.
- Reject vague "improve X" tickets, duplicate requests, and implementation detail that belongs in a PR instead of an issue.
- When the repo has no issues and the user asks for a fresh audit, follow `.agents/PROMPT.md`: create exactly 3 improvement issues and 1 feature-request issue.

## Stop Conditions

- Stop and report a blocker if `gh auth status` or `gh issue list` fails.
- Stop and ask for a scope change if the user switches from backlog creation to implementation.
- Stop without filing when existing open issues already cover the meaningful candidates or when new candidates would be lower value than the current backlog.
