---
name: hivemind-skill-capture
description: Create or update project-local Codex skills from durable Hivemind decisions, repeated workflows, user preferences, architecture rules, security boundaries, UI direction, homelab assumptions, or new asks that should be easy to replicate with little context in future sessions.
---

# Hivemind Skill Capture

## Rule

When a user gives a durable preference, project direction, repeated workflow, security invariant, UI standard, or implementation process, capture it as a project-local skill if future agents would benefit from it.

## Where

Create skills under:

```text
.agents/skills/<skill-name>/
```

Each skill needs:

- `SKILL.md`
- Optional `agents/openai.yaml` with short UI metadata

## What To Capture

Capture:

- Security boundaries.
- Self-hosted/homelab assumptions.
- Frontend taste and rejection rules.
- Agent/task/schedule/heartbeat workflows.
- Shipping and verification loops.
- Reusable implementation checklists.

Do not capture:

- One-off bugs.
- Temporary local paths unless useful for the repo.
- Generic advice Codex already knows.
- Secrets or private credentials.

## Skill Quality Bar

- Keep frontmatter description trigger-rich.
- Keep the body concise and procedural.
- Prefer project-specific instructions over generic principles.
- Include verification steps when the skill affects code.
- Validate with `quick_validate.py` when available.

## After Creating Skills

- Add or update `AGENTS.md` if the skill changes repo behavior.
- Run skill validation.
- Commit the skill with nearby related changes.

