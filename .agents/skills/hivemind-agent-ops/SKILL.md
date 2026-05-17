---
name: hivemind-agent-ops
description: Implement or revise Hivemind agent operations, task management, schedules, cron-like intervals, heartbeats, audit visibility, and swarm coordination. Use when working on agents, tasks, task status, assignment, schedules, due schedule execution, heartbeat events, worker coordination, or low-context agent communication.
---

# Hivemind Agent Ops

## Model

Hivemind coordinates many action-capable agents. Operators need to see which agent is assigned, what it is trying to do, what credential capability it needs, and whether it is still alive.

## Agents

- Track name, role, provider, model, status, and system prompt.
- Keep communication short and actionable.
- Prefer explicit statuses such as `idle`, `queued`, `running`, `blocked`, `done`, and `failed`.
- Do not hide agent IDs; operators need inspectable state.

## Tasks

- Tasks should carry title, description, status, priority, assigned agent, credential intent, action, and heartbeat expectations.
- Status transitions must be explicit and auditable.
- If a task uses a credential, connect the task to the credential policy and lease flow rather than bypassing the broker.

## Schedules

- Schedules are local interval jobs in the single container.
- Store interval, enabled state, next run, last run, task template, assigned agent, credential, action, and intent.
- Running a due schedule creates a normal task and records audit.
- Avoid claiming full cron syntax unless implemented.

## Heartbeats

- Heartbeats prove work is still alive.
- Store task, agent, note, timestamp, and next expected heartbeat when applicable.
- Surface stale or missing heartbeats in the UI before claiming production readiness.

## UI Requirements

- Show tasks, schedules, heartbeats, and audit as operational state, not marketing content.
- Make due schedules and stale heartbeats visible.
- Provide controls for creating a task, changing task status, recording heartbeat, creating schedule, and running due schedules.

