---
name: hivemind-shipping-loop
description: Drive Hivemind toward a shippable open-source single-container app. Use when planning or implementing broad app work, release readiness, Docker packaging, README updates, tests, browser verification, commit checkpoints, or when the user asks to keep churning until the app is ready to ship.
---

# Hivemind Shipping Loop

## Operating Loop

1. Choose the next missing product surface.
2. Implement the smallest complete backend and frontend path.
3. Add tests for security boundaries and core behavior.
4. Run the full test suite.
5. Verify the local app in the browser when UI changed.
6. Commit a checkpoint.
7. Repeat until the app is coherent.

## Ship Criteria

- Single-container run path works.
- Setup/login works with username/password.
- Persistent SQLite database works across restarts.
- Agents, credentials, leases, tasks, schedules, heartbeats, and audit all have UI and API coverage.
- Credentials remain brokered and redacted.
- README explains local run, Docker run, config, and security model accurately.
- Tests pass.
- UI looks self-hosted and technical, not generic SaaS.

## Commit Rule

Commit often. Make a checkpoint after each meaningful feature, security boundary, or cleanup. Do not include caches, local databases, virtualenvs, or unrelated local skills.

## Verification Commands

Use:

```bash
.venv/bin/python -m pytest
python3 -m compileall src tests
```

For frontend work, start the app and verify in the browser at `http://127.0.0.1:8000/`.

