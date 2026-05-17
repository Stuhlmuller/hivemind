---
name: hivemind-jit-credentials
description: Implement, review, or revise Hivemind credential security, credential policies, intent validation, JIT leases, brokered actions, secret references, audit events, and provider credentials. Use when working on credentials, leases, scoped access, credential UI, secret redaction, policy denial paths, or AI-provider-backed intent review.
---

# Hivemind JIT Credentials

## Security Boundary

Agents never receive raw credentials. Agents request a capability from the credential broker. The broker validates agent, action, intent, TTL, and policy before issuing a short-lived lease.

## Required Behavior

- Store secret references only, such as `env://`, `file://`, `vault://`, or `oauth://`.
- Never return raw secret values from API responses, logs, audit events, tests, or UI.
- Redact secret references in public views.
- Deny by default when agent, action, intent, credential, TTL, or lease token is invalid.
- Scope every lease to one credential, one agent, one action, and one expiry.
- Store a hash of lease tokens; only show token previews after creation.
- Audit issued leases, denied leases, and brokered actions.

## Policy Checks

Validate:

- Agent exists.
- Credential exists.
- Agent is listed in credential policy.
- Requested action is listed in credential policy.
- Intent is present when required.
- Requested TTL is capped by credential max TTL.
- Brokered action matches an active, unexpired, unrevoked lease.

## UI Checks

- Show credential provider, secret reference preview, allowed agents, allowed actions, max TTL, and require-intent.
- Show lease status, action, expiry, agent, credential, and token preview.
- Make denial reasons visible in audit.
- Do not make credential creation look like storing a raw API key unless the backend truly encrypts and stores secrets.

## Tests

Cover:

- Allowed lease creation.
- Denial for wrong agent.
- Denial for wrong action.
- Denial for short intent when required.
- Expired or revoked lease rejection.
- Action mismatch rejection.
- No raw secret exposure in public views.
