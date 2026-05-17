---
name: hivemind-idea-issues
description: Brainstorm and refine new Hivemind product, security, UX, operations, and developer-experience ideas, then turn the best ideas into concrete GitHub issues instead of implementation work. Use when the user asks for new ideas, roadmap or backlog suggestions, gap analysis, what to build next, or wants opportunities captured as issues without starting development.
---

# Hivemind Idea Issues

## Goal

Turn Hivemind idea generation into backlog capture. Bias toward finding more grounded issues because there is almost always more useful work to record. Create issues only. Do not write product code, create branches, open PRs, or start implementation unless the user explicitly changes scope.

## Workflow

1. Inspect the repo surfaces that match the user's prompt before inventing ideas.
2. Widen the audit past the first obvious surface. Check adjacent product, security, reliability, operations, docs, tests, release, and developer-experience gaps before concluding the backlog is exhausted.
3. Verify GitHub access with `gh auth status`.
4. Read the current backlog with `gh issue list --state all --limit 100`.
5. Reject duplicates by comparing candidate ideas against open and closed issues.
6. Ground each proposed idea in a concrete repo gap, risk, inconsistency, or missing capability.
7. Keep looking for additional distinct issues after the first few wins. Prefer several focused tickets over one umbrella issue when the repo supports that split.
8. Create one GitHub issue per accepted idea with `gh issue create`.
9. Stop after issue creation and report the issue numbers and links.

## Issue Bar

- Keep each issue focused on one problem or opportunity.
- Cover the problem or opportunity, why it matters, the expected outcome, and evidence from the repo.
- Prefer security, reliability, release readiness, operator visibility, credential safety, self-hosted UX, agent coordination, and developer-experience gaps when the codebase supports them.
- If another grounded, non-duplicate issue exists, file it instead of stopping early or hiding it inside a broad umbrella ticket.
- Reject vague "improve X" tickets, duplicate requests, and implementation detail that belongs in a PR instead of an issue.
- When the repo has no issues and the user asks for a fresh audit, follow `.agents/PROMPT.md`: create exactly 8 improvement issues and 2 feature-request issues.

## Stop Conditions

- Stop and report a blocker if `gh auth status` or `gh issue list` fails.
- Stop and ask for a scope change if the user switches from backlog creation to implementation.
- Stop without filing filler tickets only after widening the audit and confirming no grounded, non-duplicate ideas remain.
