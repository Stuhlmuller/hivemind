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

## Nix Dev Shell

The repository flake is primarily used to keep Ralph and agent runs aligned on a
repeatable CLI set:

```bash
nix flake check
nix develop
```

If `nix flake check` passes but `nix develop` fails with `Problem with the SSL
CA cert (path? access rights?)`, the repo flake is usually fine and the local
multi-user Nix installation is broken instead. Run:

```bash
./scripts/diagnose-nix-develop.sh
```

On macOS, one common failure mode is a broken
`/etc/ssl/certs/ca-certificates.crt` symlink that still points through
`/etc/static` to a missing Nix store path. When the diagnosis script reports
that state and `/nix/var/nix/profiles/default/etc/ssl/certs/ca-bundle.crt`
exists, repair it with:

```bash
sudo rm /etc/ssl/certs/ca-certificates.crt
sudo ln -s /nix/var/nix/profiles/default/etc/ssl/certs/ca-bundle.crt /etc/ssl/certs/ca-certificates.crt
nix develop --command bash -lc "printf 'dev-shell-ok\n'"
```

If the repair does not hold or `/etc/static` still points at a missing store
path, repair or reinstall the macOS multi-user Nix daemon installation. This is
a machine-local problem, not a Hivemind flake problem.

## Login

There is no baked-in default account. On first run, Hivemind shows a setup
screen. The first username/password you submit becomes the local admin account.
The setup form starts blank and requires an operator-entered password. After
setup completes, use the same username and password on the login screen.

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
