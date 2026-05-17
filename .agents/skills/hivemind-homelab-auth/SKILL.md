---
name: hivemind-homelab-auth
description: Implement or revise Hivemind authentication, setup, session, account, and self-hosted access flows. Use when working on login, setup, username/password auth, cookies, local admin accounts, homelab deployment posture, single-user or small-team self-hosted assumptions, or when removing B2B SaaS/email/account-management assumptions from Hivemind.
---

# Hivemind Homelab Auth

## Intent

Hivemind is self-hosted homelab software. Auth should feel like a local admin console, not a B2B SaaS signup flow.

## Rules

- Use `username` and `password`, not email, for first-party login.
- The first local user becomes admin during setup.
- Store password hashes only. Use PBKDF2, bcrypt, argon2, or a similarly appropriate slow hash.
- Issue HttpOnly session cookies. Use `Secure` cookies when configured for HTTPS.
- Keep setup state explicit: before setup, show setup; after setup, show login.
- Do not add teams, invites, billing, organizations, tenant IDs, workspace branding, or email verification unless explicitly requested.
- Do not imply cloud identity or managed accounts.

## API Expectations

- `GET /setup-state` returns whether setup is complete.
- `POST /auth/setup` accepts `username` and `password`.
- `POST /auth/login` accepts `username` and `password`.
- `POST /auth/logout` clears the session.
- `GET /me` returns `id`, `username`, and `role`.
- Operational endpoints require an authenticated session.

## UI Expectations

- Login copy should say local, self-hosted, or admin console when helpful.
- Avoid marketing text. The auth screen should get the operator into the control plane quickly.
- Username fields use `autocomplete="username"`.
- Password fields use password autocomplete appropriate to setup or login context.

## Verification

- Test unauthenticated endpoints return 401, not validation noise.
- Test setup creates the first admin.
- Test login works with username/password.
- Test source has no accidental `email` auth payloads.

