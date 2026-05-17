# Hivemind

Hivemind is a security-focused, bee-themed agent runtime for coordinating many
action-capable subagents without giving those agents direct access to
credentials.

The current implementation includes:

- A swarm agent registry.
- A same-container frontend served from `/`.
- Local username/password setup and login.
- Environment-based intent reviewer configuration.
- SQLite persistence.
- A credential broker that stores secret references instead of secret values.
- Short-lived, scoped leases after policy and intent validation.
- Tasks, schedules, heartbeats, and an audit trail.
- A FastAPI HTTP surface that runs as a single container.

## Run Locally

```bash
pip install -e ".[dev]"
uvicorn hivemind.api:create_app --factory --reload
```

Then open `http://localhost:8000/`.

The API docs are available at `http://localhost:8000/docs`.

## Login

There is no baked-in default account. On first run, Hivemind shows a setup
screen. The first username/password you submit becomes the local admin account.

For local development, the setup form is prefilled with:

```text
username: admin
password: hivemind-password
```

Those values are only UI defaults for a fresh local database. Change them during
setup for any real self-hosted install. After setup completes, use the same
username and password on the login screen.

Optional intent reviewer configuration:

```bash
export HIVEMIND_INTENT_REVIEWER_PROVIDER=openrouter
export HIVEMIND_INTENT_REVIEWER_MODEL=anthropic/claude-sonnet-4
export HIVEMIND_INTENT_REVIEWER_CREDENTIAL_REF=env://OPENROUTER_API_KEY
```

## Container

```bash
docker build -t hivemind .
docker run --rm -p 8000:8000 -v hivemind-data:/data hivemind
```

## Security Model

Agents never receive raw credentials. They request a capability from the
credentials service with an explicit action and intent. The service validates
that request against credential policy, creates a narrow lease when allowed,
and rejects any later action that does not match the lease.

The current policy engine is deterministic so the core can be tested locally.
The intended next step is to add provider-backed intent review using the user's
configured AI model while preserving the same JIT credential lease boundary.
