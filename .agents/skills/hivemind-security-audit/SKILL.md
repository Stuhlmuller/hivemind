---
name: hivemind-security-audit
description: Run a mandatory Hivemind security and architecture audit after every implementation change. Use after code, frontend, backend, auth, credential, lease, agent, task, schedule, heartbeat, storage, Docker, README, AGENTS.md, or project skill changes to check secret handling, JIT credential boundaries, auth/session safety, persistence, auditability, tests, architecture, and release risk before finishing.
---

# Hivemind Security Audit

## Purpose

Use this skill after every Hivemind implementation change before finalizing, committing, or handing work back. The audit exists to keep Hivemind secure by default, deny-by-default, self-hosted, and well-architected around brokered JIT credentials.

## Workflow

1. Review the exact diff and classify the touched surfaces: auth/session, credentials/JIT leases, agents/tasks/schedules/heartbeats, API, persistence, frontend, Docker/config, docs, or skills.
2. Run the checklist sections that match the touched surfaces, plus the universal checks.
3. Treat blocker findings as implementation work, not notes. Fix them before finishing unless the user explicitly accepts the residual risk.
4. Run focused verification for the changed surface. Use broader tests when a shared boundary changed.
5. In the final response, state the security audit result, tests run, fixes made, and any remaining risk.

## Universal Checks

- No raw secret, token, OAuth value, password, session cookie, or credential material is printed, logged, embedded in frontend state, committed to fixtures, or written to audit events.
- Security-sensitive paths are deny-by-default. Missing policy, missing auth, expired lease, mismatched agent, mismatched credential, or unsupported action must fail closed.
- Public claims in README, UI copy, skills, and comments match shipped behavior. Do not imply production-grade encryption, provider support, sandboxing, or policy enforcement until implemented.
- New code follows the existing architecture and keeps trust boundaries narrow. Avoid global mutable secret state and broad helper APIs that bypass the credential service.
- Errors are useful but do not reveal internal secrets, hashes, database paths, stack traces, or provider credential details.

## Auth And Session Checks

- Hivemind uses local username/password auth, not email-first SaaS identity.
- There is no baked-in default account. Setup is available only when no local users exist.
- Passwords are hashed with a slow password hash and never stored or displayed in plaintext.
- Session cookies are HttpOnly, scoped, and invalidated on logout.
- Operational APIs require an authenticated session and return a clean unauthorized response when unauthenticated.

## Credential And Lease Checks

- Agents never receive raw credentials. They request brokered action through the credential service.
- Credential records store references, metadata, policy, and redacted displays rather than secret values.
- JIT leases are scoped to one agent, one credential, one action intent, and a short TTL.
- Lease issuance validates agent identity, credential policy, requested action, TTL bounds, and intent before granting access.
- Lease use rejects expired, revoked, mismatched, or overbroad access.
- Token material is shown only when unavoidable and only once; persisted values are hashed or otherwise non-recoverable.
- Every credential decision creates an audit record with redacted context.

## Agent, Task, Schedule, And Heartbeat Checks

- Agent communication stays concise and actionable; do not add large hidden prompts or vague autonomous behavior.
- Task assignment, status changes, schedule firing, and heartbeat updates are auditable.
- Cron and heartbeat execution cannot bypass auth, credential policy, or lease validation.
- Failed or stale agents are visible without exposing secrets.
- Scheduler behavior is deterministic across restarts and does not duplicate work unexpectedly.

## API, Persistence, And Frontend Checks

- Validate inputs at the API boundary and normalize IDs before use.
- Database schema changes include safe migrations and preserve existing local data.
- Frontend code does not store secrets in localStorage, render raw HTML from untrusted content, or expose hidden privileged actions.
- UI remains self-hosted and technical. Avoid SaaS account language, fake enterprise copy, and unimplemented security promises.
- Docker/config changes preserve the single-container self-hosted deployment path.

## Verification

- Run focused backend tests for API, auth, store, scheduler, credential, or lease changes.
- Run frontend build checks for UI changes. Reserve Codex browser validation for the main-branch scout lane when shipped UI behavior needs live confirmation.
- Run `quick_validate.py` for skill changes.
- Run documentation or README-focused tests when docs describe login, setup, security, or deployment behavior.
- If verification cannot run, say why and identify the residual risk.

## Blockers

Fix these before finishing:

- Raw credential exposure to an agent, UI, logs, tests, docs, or audit events.
- Auth bypass or operational endpoint reachable without a valid session.
- Unscoped, long-lived, reusable, or policy-free credential lease.
- User-configured security policy ignored or silently weakened.
- Failing verification on a changed security boundary.
- Public documentation or UI claiming a security capability that is not implemented.
