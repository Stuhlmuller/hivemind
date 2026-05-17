---
name: hivemind-browser-scope
description: Control Codex browser tool usage in Hivemind. Use when deciding whether to open the local app in the Codex browser, when changing browser-verification guidance, or when assigning scout versus development verification responsibilities.
---

# Hivemind Browser Scope

## Rule

Reserve the Codex browser tool for the main-branch scout lane only. That lane
may use the browser tool to validate shipped behavior on the default branch and
turn concrete findings into new GitHub issues.

## Do Not Use The Browser Tool For

- Feature-request drafting or speculative product exploration.
- General development or issue implementation.
- PR shepherd, CI triage, or review updates.
- Worker, reviewer, QA, or feature-requester subagents attached to in-flight
  issue branches.
- Local UI polish loops during feature work.

## Preferred Alternatives

- Use automated tests, logs, screenshots already provided, static inspection,
  and targeted code review during development.
- If live UI behavior still looks suspicious during development, note the gap
  and leave browser validation to the main-branch scout lane.

## Alignment

Keep these aligned with this rule:

- `AGENTS.md`
- `.agents/PROMPT.md`
- `.agents/PROMPT-subagents.md`
- `.agents/PROMPT-scout.md`
- `.agents/PROMPT-reviewer.md`
- `.agents/PROMPT-worker.md`
- `.agents/PROMPT-feature-requester.md`
- `.agents/PROMPT-pr-shepherd.md`
- Any browser-related shipping, UI, or audit skills

## Verification

Run:

```bash
bash tests/test_ralph_loop.sh
bash .agents/verify-ralph-worktree.sh
bash .agents/verify-swarm.sh
qlty check AGENTS.md .agents/PROMPT.md .agents/PROMPT-subagents.md .agents/PROMPT-scout.md .agents/PROMPT-reviewer.md .agents/PROMPT-worker.md .agents/PROMPT-feature-requester.md .agents/PROMPT-pr-shepherd.md .agents/skills/hivemind-browser-scope/SKILL.md
```
