---
name: hivemind-ui-no-slop
description: Prevent and remove AI slop from Hivemind frontend work. Use before designing, implementing, reviewing, or revising Hivemind UI, CSS, copy, layouts, screenshots, dashboards, login/setup flows, task views, credential views, scheduler views, heartbeat views, or agentic control surfaces so the interface feels technical, open-source, self-hosted, security-focused, agentic, and new rather than generic SaaS, decorative, beige, cute, or marketing-heavy.
---

# Hivemind UI No Slop

## North Star

Build a technical self-hosted control plane for agent swarms and credential brokering. The UI should feel like something security engineers, infra maintainers, and open-source operators would trust enough to run on their own machine.

## Hard Requirements

- Make the first screen the product, not a landing page.
- Prioritize operational density: agents, tasks, credentials, leases, schedules, heartbeats, audit events, and policy state must be visible without fluffy explanation.
- Show real system state: IDs, statuses, TTLs, scopes, next runs, last heartbeats, denied decisions, and audit reasons.
- Treat credential UI as security UI. Never show raw secret values. Make secret references visibly redacted.
- Make JIT access legible: request, decision, lease, expiry, action, audit.
- Use a design language closer to developer tools, GitHub, Linear, Grafana, Kubernetes dashboards, or local admin consoles than consumer SaaS.
- Use the bee/hive theme only as naming and small structural cues. No cute mascot energy. No honey-colored blanket over the whole app.

## Visual Direction

Use:

- Compact layout with persistent navigation or clear workspace regions.
- Dense tables, split panes, inspector panels, logs, timelines, code-like blocks, status chips, and filterable lists.
- Neutral dark or high-contrast light surfaces with restrained accent colors.
- Monospace only for IDs, tokens, policy snippets, logs, schedules, and payloads.
- Thin borders, small radii, crisp spacing, and clear hover/focus states.
- Graph or topology elements only when they expose relationships between agents, credentials, tasks, and leases.

Avoid:

- Giant marketing hero sections.
- Beige, cream, honey, tan, amber-dominant palettes.
- Decorative honeycomb blobs, oversized hex grids, clip-art bees, playful illustrations, gradient orbs, or empty visual set pieces.
- Cards inside cards, billboard headings in operational panels, or UI text that explains obvious controls.
- Generic phrases like "seamless", "powerful", "AI-powered", "secure by design", "unlock", "supercharge", "mission control", or "enterprise-grade" unless backed by specific shipped behavior.

## Information Architecture

Prefer these top-level surfaces:

- Overview: live counts, recent denials, active leases, due schedules, stale heartbeats.
- Agents: registry, model/provider, status, assigned tasks, allowed credentials.
- Credentials: provider, secret ref preview, allowed agents/actions, max TTL, require-intent flag.
- Tasks: queue, assignment, status transitions, intent, credential/action requested, heartbeat state.
- Schedules: interval, next run, last run, enabled state, generated tasks.
- Leases: credential, agent, action, expiry, status, token preview only.
- Audit: append-only decisions with actor, target, reason, metadata, timestamp.

## Copy Rules

- Write labels like an admin console, not a pitch deck.
- Prefer nouns and state over adjectives.
- Replace "Coordinate work, leases, schedules, and heartbeats" with concrete labels like "Active leases", "Denied requests", "Stale heartbeats", or "Due schedules".
- Keep helper text short and security-relevant.
- If a feature is fake, remove it. If it is planned, label it as not implemented.

## Implementation Checks

Before shipping frontend changes:

1. Open the app in the browser at desktop and mobile widths.
2. Complete setup or login.
3. Create or view an agent.
4. Create or view a credential without exposing the raw secret.
5. Request a lease and confirm expiry/action/status are visible.
6. Perform an action and confirm the audit stream records it.
7. Create a task, heartbeat it, and change its status.
8. Create a schedule and run due schedules.
9. Capture screenshots and reject the UI if it reads as generic SaaS, cute bee theme, or marketing page.

## Rejection Test

If the UI could be renamed to any random AI dashboard without changing the layout, it fails.

If a security engineer cannot tell which agent can use which credential for which action and for how long, it fails.

If the page looks friendlier than it looks inspectable, it fails.

