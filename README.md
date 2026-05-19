# Hivemind

Hivemind is a security-focused agent runtime for tracking agents, tasks, and
brokered credential leases without storing raw secrets on agent records.

The current implementation includes:

- An agent registry.
- A same-container frontend served from `/`.
- Local username/password setup and login.
- Redacted intent reviewer configuration exposed through `/config`.
- SQLite persistence.
- A credential broker that stores secret references plus optional broker-encrypted secret material.
- Short-lived, scoped leases after policy and intent validation.
- Tasks, schedules with explicit catch-up policies, heartbeats, and an audit trail.
- A FastAPI HTTP surface that runs as a single container.

Schedules expose three operator-visible catch-up policies: `run_once` executes
one immediate recovery task and resets cadence from now, `skip_missed` drops
older missed slots while keeping the original cadence, and `backfill` creates
one task per overdue slot before resuming the next scheduled run. Long backfill
windows are processed in bounded batches so restarts remain responsive.

## Start The Dev Server

The fastest path is the Nix dev shell:

```bash
nix develop
hivemind-dev
```

`nix develop` prepares the local `.data` directory, points
`HIVEMIND_DB_PATH` at `.data/hivemind.db`, and prints the local app URL.
`hivemind-dev` starts the FastAPI server with reload enabled and opts that
server process into `HIVEMIND_DEVELOPMENT_MODE=true` for plain HTTP login.
Override `HIVEMIND_HOST`, `HIVEMIND_PORT`, or `HIVEMIND_DB_PATH` before
running it if you need a different local target.

If you are outside the Nix shell, use the same environment explicitly:

```bash
pip install -e ".[dev]"
mkdir -p .data
export HIVEMIND_DEVELOPMENT_MODE=true
export HIVEMIND_DB_PATH="$PWD/.data/hivemind.db"
uvicorn hivemind.api:create_app --factory --reload --host 127.0.0.1 --port 8000
```

Then open `http://localhost:8000/`.
Local processes default to `.data/hivemind.db` when `HIVEMIND_DB_PATH` is not
set; the explicit export above keeps the active path visible and easy to
override.

`HIVEMIND_DEVELOPMENT_MODE=true` is required for plain HTTP local development.
Outside explicit development mode, Hivemind marks auth session cookies `Secure`,
so browser setup and login require HTTPS.

The API docs are available at `http://localhost:8000/docs`.

## Nix Dev Shell

The repository flake is the source of truth for the local development toolchain.
It provides Python, pytest, uvicorn, GitHub CLI, ripgrep, and the
`hivemind-dev` launcher:

```bash
nix flake check
nix develop
```

After the shell opens, run `hivemind-dev` to start the reload server or
`pytest` to run the backend tests.

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

## Continuous Integration

GitHub Actions runs the repo-owned baseline on pushes and pull requests to
`main`: `nix flake check --no-write-lock-file`,
`nix develop --command pytest -q`, shell script syntax checks,
`nix develop --command bash .agents/verify-swarm.sh`, and a tracked-file check
for unresolved merge conflict markers, including diff3 markers. Repository
maintainers should require the `CI / Repository hygiene` and
`CI / Nix and tests` status checks before merging changes into `main`.

`qlty check` remains a local handoff gate because `qlty` is currently listed as
host-managed in `.agents/TOOLS.md` instead of being provided by `flake.nix`.
The pre-commit hooks remain contributor opt-in for the same reason; promote
selected hooks into the required CI baseline only after their tools are added
to the Nix dev shell.

## Dev Server Login

There is no baked-in default account. On first run, Hivemind shows a setup
screen. The first username/password you submit becomes the local admin account.
The setup form starts blank and requires an operator-entered password with at
least 12 non-whitespace characters. After setup completes, use the same
username and password on the login screen.

To start over during local development, stop the dev server and point
`HIVEMIND_DB_PATH` at a new file before restarting. The dev shell and the
explicit quickstart both default the database to `.data/hivemind.db`.

Optional intent reviewer configuration:

```bash
export HIVEMIND_INTENT_REVIEWER_PROVIDER=openrouter
export HIVEMIND_INTENT_REVIEWER_MODEL=anthropic/claude-sonnet-4
export HIVEMIND_INTENT_REVIEWER_CREDENTIAL_REF=env://OPENROUTER_API_KEY
```

These values are visible to operators through `/config`, with the credential
reference redacted. The current policy engine still uses deterministic local
checks for agent scope, action scope, TTL, and intent length.

Optional agent provider configuration:

```bash
export HIVEMIND_AGENT_PROVIDER_OPENROUTER_MODEL=anthropic/claude-sonnet-4
export HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_ID=cred_provider_openrouter
```

`/config` exposes a provider catalog for the deterministic local provider plus
OpenAI, Codex, Claude, Gemini, OpenRouter, Bedrock, Hugging Face, and Ollama.
Agents select a provider and model on their agent record. Task execution goes
through the provider adapter registry, which currently ships only the
deterministic local adapter; remote providers fail closed until an adapter is
registered in code.
Provider credentials must be credential records managed by the broker. The
provider config points at a credential ID; the underlying secret reference stays
on the credential record, is redacted in public views, and is authorized through
a short-lived broker lease before adapter handoff.

Optional broker secret storage and Codex subscription OAuth configuration:

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

With those variables set, the credentials console can either store a secret in
broker-owned encrypted storage or bootstrap a Codex subscription OAuth
credential. Broker-managed secrets are persisted as ciphertext and exposed only
as redacted `secret://...` references in public views. The OAuth browser
callback stores the token bundle in the same encrypted broker store and creates
an `oauth://codex/...` credential reference, while public API responses
continue to expose only redacted refs.

## Declarative Config

Authenticated operators can export and import reproducible runtime config:

```bash
curl -sS -b cookies.txt http://localhost:8000/declarative-config > hivemind.config.json
curl -sS -b cookies.txt -H "content-type: application/json" \
  -d '{"dry_run": true, "config": '"$(cat hivemind.config.json)"'}' \
  http://localhost:8000/declarative-config/import
```

The config contains agents, credential policies with explicit approval-gated
action lists, and schedules with explicit catch-up policies and nested task
templates. Credential entries include secret references such as
`env://HIVEMIND_REPO_READER_TOKEN`, not raw tokens or encrypted OAuth payloads.
Declarative imports accept external secret refs only; broker-generated
`secret://` refs and broker-backed `oauth://` refs are not portable config.
Dry-run import validates references, TTLs, interval bounds, schedule catch-up
policies and task templates, and credential policy compatibility without
writing to SQLite. Apply with `"dry_run": false` after the operator has
provisioned the referenced external secrets. Applied imports create or update
matching objects; they do not delete agents, credentials, or schedules omitted
from the config.

See `docs/declarative-config.example.json` for a complete example.

## Container

```bash
docker build -t hivemind .
docker run --rm \
  -p 8000:8000 \
  -v hivemind-data:/data \
  -e HIVEMIND_DEVELOPMENT_MODE=true \
  hivemind
```

This is the local container smoke-test path. Open `http://localhost:8000/`,
complete the first-run admin setup if the `hivemind-data` volume is empty, and
then log in with that same username/password on later starts.

For the actual self-hosted container, keep the same `/data` volume but run the
app behind TLS or another HTTPS terminator and leave
`HIVEMIND_DEVELOPMENT_MODE` unset:

```bash
docker run -d \
  --name hivemind \
  --restart unless-stopped \
  -p 8000:8000 \
  -v hivemind-data:/data \
  hivemind
```

Auth session cookies are `Secure` by default in this mode, so use the HTTPS
URL from your reverse proxy when completing setup or logging in. The first
username/password entered for an empty `/data` volume becomes the local admin;
there are no default container credentials.

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

Inside a container, the image sets `HIVEMIND_DB_PATH=/data/hivemind.db` unless
you override it. A typical volume-backed flow looks like:

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
the backup file itself as a sensitive operator artifact. Tasks and schedules
that pointed at an excluded OAuth credential are restored with that credential
link cleared so operators can reconnect the capability deliberately. Run
restore while the instance is stopped or otherwise quiesced so you do not race
live API traffic while replacing persisted state.

## Security Model

To report a vulnerability, see [SECURITY.md](SECURITY.md).

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
