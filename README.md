# Hivemind

Hivemind is a security-focused, bee-themed agent runtime for coordinating many
action-capable subagents without giving those agents direct access to
credentials.

The first implementation includes:

- A swarm agent registry.
- A same-container frontend playground served from `/`.
- Environment-based intent reviewer configuration.
- A credential vault that stores secret references instead of secret values.
- A credential service that issues short-lived, scoped leases after policy and
  intent validation.
- An audit trail for lease decisions and brokered credential actions.
- A FastAPI HTTP surface that runs as a single container.

## Run Locally

```bash
pip install -e ".[dev]"
uvicorn hivemind.api:create_app --factory --reload
```

Then open `http://localhost:8000/docs`.

The frontend playground is available at `http://localhost:8000/`.

Optional intent reviewer configuration:

```bash
export HIVEMIND_INTENT_REVIEWER_PROVIDER=openrouter
export HIVEMIND_INTENT_REVIEWER_MODEL=anthropic/claude-sonnet-4
export HIVEMIND_INTENT_REVIEWER_CREDENTIAL_REF=env://OPENROUTER_API_KEY
```

## Container

```bash
docker build -t hivemind .
docker run --rm -p 8000:8000 hivemind
```

## Security Model

Agents never receive raw credentials. They request a capability from the
credentials service with an explicit action and intent. The service validates
that request against credential policy, creates a narrow lease when allowed,
and rejects any later action that does not match the lease.

The current policy engine is deterministic so the core can be tested locally.
The intended next step is to add provider-backed intent review using the user's
configured AI model while preserving the same JIT credential lease boundary.
