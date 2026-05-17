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

For plain HTTP local development, set `HIVEMIND_DEVELOPMENT_MODE=true` before
launching the app. Outside explicit development mode, Hivemind marks auth
session cookies `Secure`, so setup/login require HTTPS.

The API docs are available at `http://localhost:8000/docs`.

## Nix Dev Shell

The repository flake is primarily used to keep the swarm loops and repo agent
runs aligned on a repeatable CLI set:

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

Optional Codex subscription OAuth configuration:

```bash
export HIVEMIND_SECRETS_KEY="<set-a-long-random-secret-key>"
export HIVEMIND_OAUTH_CODEX_AUTHORIZE_URL="https://your-oauth-provider.example/oauth/authorize"
export HIVEMIND_OAUTH_CODEX_TOKEN_URL="https://your-oauth-provider.example/oauth/token"
export HIVEMIND_OAUTH_CODEX_CLIENT_ID="your-client-id"
# Optional for confidential clients:
export HIVEMIND_OAUTH_CODEX_CLIENT_SECRET="<set-client-secret-if-needed>"
# Optional override; defaults to: openid profile email offline_access
export HIVEMIND_OAUTH_CODEX_SCOPES="openid profile email offline_access"
```

With those variables set, the credentials console exposes a dedicated Codex
subscription OAuth flow. The browser callback stores the token bundle in
broker-owned encrypted storage and creates an `oauth://codex/...` credential
reference, while public API responses continue to expose only redacted refs.

## Container

```bash
docker build -t hivemind .
docker run --rm -p 8000:8000 -v hivemind-data:/data hivemind
```

Run the container behind TLS or another HTTPS terminator in normal deployments.
Auth session cookies are `Secure` by default. Use
`HIVEMIND_DEVELOPMENT_MODE=true` only for local HTTP development.

GitHub Actions also builds this image on pull requests and publishes it to
GitHub Container Registry from `main` and version tags as
`ghcr.io/<owner>/hivemind`.

## Backup And Restore

Use the packaged CLI for operator-managed logical backups of the SQLite-backed
runtime state:

```bash
hivemind backup ./hivemind-backup.json
hivemind restore ./hivemind-backup.json
```

Inside a container, the default database path is `/data/hivemind.db` unless you
override `HIVEMIND_DB_PATH`. A typical volume-backed flow looks like:

```bash
docker exec hivemind hivemind backup /data/backups/hivemind-backup.json
docker exec hivemind hivemind restore /data/backups/hivemind-backup.json
```

The logical bundle is versioned and restore rejects incompatible backup format
versions. The bundle includes durable operator state such as users, password
hashes, agents, tasks, schedules, audit history, and credential secret refs for
`env://`, `file://`, and `vault://` credentials. It intentionally excludes
active sessions, live leases, pending OAuth states, and broker-owned OAuth
token material, so reconnect OAuth-backed credentials after a restore and treat
the backup file itself as a sensitive operator artifact. Run restore while the
instance is stopped or otherwise quiesced so you do not race live API traffic
while replacing persisted state.

## Security Model

Agents never receive raw credentials. They request a capability from the
credentials service with an explicit action and intent. The service validates
that request against credential policy, creates a narrow lease when allowed,
and rejects any later action that does not match the lease.

The current policy engine is deterministic so the core can be tested locally.
When `HIVEMIND_INTENT_REVIEWER_PROVIDER` is set to a non-local value, lease
requests flow through a fail-closed provider reviewer interface after the same
deterministic policy checks. Provider adapters can be registered in code so
the broker can keep secrets and credential refs out of agents, the frontend,
and public API responses while preserving the local deterministic reviewer for
offline and self-hosted operation. The default app path keeps non-local
reviewer configs fail-closed until a provider adapter is registered; setting a
provider by environment alone does not pass raw provider secrets to agents or
bypass the broker.
