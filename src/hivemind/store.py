from __future__ import annotations

import base64
from collections.abc import Mapping
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

from hivemind.config import HivemindConfig
from hivemind.oauth import SecretBox
from hivemind.policy import PolicyEngine, PolicyReviewInput, ProviderIntentReviewer
from hivemind.secret_refs import preview_secret_ref, validate_secret_ref

SCHEDULE_BACKFILL_BATCH_LIMIT = 100
SCHEDULE_CATCH_UP_POLICIES = ("skip_missed", "run_once", "backfill")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def require_aware_utc(value: str, *, field_name: str) -> datetime:
    try:
        parsed = parse_dt(value)
    except (TypeError, ValueError) as exc:
        raise StoreError(f"schedule {field_name} must be a valid ISO datetime") from exc
    if parsed is None:
        raise StoreError(f"schedule {field_name} is required")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StoreError(f"schedule {field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def loads(value: str | None, default: Any) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 240_000)
    return "pbkdf2_sha256$240000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


class StoreError(ValueError):
    pass


class StoreNotFoundError(StoreError):
    pass


class StoreValidationError(StoreError):
    pass


LEASE_DENIED_EVENT = "credential.lease.denied"
ACTION_DENIED_EVENT = "credential.action.denied"
TASK_BY_ID_QUERY = "SELECT * FROM tasks WHERE id = ?"
SCHEDULE_BY_ID_QUERY = "SELECT * FROM schedules WHERE id = ?"


@dataclass(frozen=True)
class SessionUser:
    id: str
    username: str
    role: str


class HivemindStore:
    def __init__(
        self,
        db_path: str | Path,
        *,
        config: HivemindConfig | None = None,
        provider_reviewers: Mapping[str, ProviderIntentReviewer] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self.config = config or HivemindConfig.from_env()
        self._policy_engine = PolicyEngine(
            self.config.intent_reviewer,
            provider_reviewers=provider_reviewers,
        )
        self._migrate()

    @classmethod
    def from_env(cls, *, provider_reviewers: Mapping[str, ProviderIntentReviewer] | None = None) -> "HivemindStore":
        config = HivemindConfig.from_env()
        path = os.getenv("HIVEMIND_DB_PATH", "/data/hivemind.db")
        if path == ":memory:":
            return cls(path, config=config, provider_reviewers=provider_reviewers)
        return cls(Path(path), config=config, provider_reviewers=provider_reviewers)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _migrate(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                  id TEXT PRIMARY KEY,
                  username TEXT NOT NULL UNIQUE,
                  password_hash TEXT NOT NULL,
                  role TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                  token_hash TEXT PRIMARY KEY,
                  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  created_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agents (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  role TEXT NOT NULL,
                  provider TEXT NOT NULL,
                  model TEXT NOT NULL,
                  status TEXT NOT NULL,
                  system_prompt TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS credentials (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  provider TEXT NOT NULL,
                  secret_ref TEXT NOT NULL,
                  allowed_agents TEXT NOT NULL,
                  allowed_actions TEXT NOT NULL,
                  approval_required_actions TEXT NOT NULL DEFAULT '[]',
                  max_ttl_seconds INTEGER NOT NULL,
                  require_intent INTEGER NOT NULL,
                  metadata TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS oauth_states (
                  id TEXT PRIMARY KEY,
                  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  provider TEXT NOT NULL,
                  pkce_verifier TEXT NOT NULL,
                  credential_payload TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS oauth_connections (
                  credential_id TEXT PRIMARY KEY REFERENCES credentials(id) ON DELETE CASCADE,
                  provider TEXT NOT NULL,
                  scopes TEXT NOT NULL,
                  token_ciphertext TEXT NOT NULL,
                  token_expires_at TEXT,
                  has_refresh_token INTEGER NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS leases (
                  id TEXT PRIMARY KEY,
                  token_hash TEXT NOT NULL UNIQUE,
                  token_preview TEXT NOT NULL,
                  credential_id TEXT NOT NULL REFERENCES credentials(id) ON DELETE CASCADE,
                  agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                  action TEXT NOT NULL,
                  intent TEXT NOT NULL,
                  ttl_seconds INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL,
                  issued_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                  id TEXT PRIMARY KEY,
                  title TEXT NOT NULL,
                  description TEXT NOT NULL,
                  status TEXT NOT NULL,
                  priority TEXT NOT NULL,
                  assigned_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
                  credential_id TEXT REFERENCES credentials(id) ON DELETE SET NULL,
                  action TEXT NOT NULL,
                  intent TEXT NOT NULL,
                  heartbeat_seconds INTEGER,
                  next_heartbeat_at TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schedules (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  enabled INTEGER NOT NULL,
                  interval_seconds INTEGER NOT NULL,
                  catch_up_policy TEXT NOT NULL DEFAULT 'run_once',
                  task_title TEXT NOT NULL,
                  task_description TEXT NOT NULL,
                  priority TEXT NOT NULL,
                  assigned_agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
                  credential_id TEXT REFERENCES credentials(id) ON DELETE SET NULL,
                  action TEXT NOT NULL,
                  intent TEXT NOT NULL,
                  next_run_at TEXT NOT NULL,
                  last_run_at TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS heartbeat_events (
                  id TEXT PRIMARY KEY,
                  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                  agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL,
                  note TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                  id TEXT PRIMARY KEY,
                  type TEXT NOT NULL,
                  actor_id TEXT NOT NULL,
                  target_id TEXT NOT NULL,
                  decision TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  metadata TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                """
            )
            self._migrate_sessions_to_token_hashes(conn)
            self._migrate_users_to_username(conn)
            self._migrate_schedules_to_catch_up_policy(conn)
            self._migrate_credentials_to_approval_actions(conn)
            self._migrate_leases_to_store_ttl(conn)

    def _migrate_sessions_to_token_hashes(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "token_hash" in columns or "token" not in columns:
            return
        legacy_rows = conn.execute("SELECT token, user_id, created_at, expires_at FROM sessions").fetchall()
        conn.execute("ALTER TABLE sessions RENAME TO sessions_legacy")
        conn.execute(
            """
            CREATE TABLE sessions (
              token_hash TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            [
                (self.hash_token(row["token"]), row["user_id"], row["created_at"], row["expires_at"])
                for row in legacy_rows
            ],
        )
        conn.execute("DROP TABLE sessions_legacy")

    def _migrate_users_to_username(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "username" in columns or "email" not in columns:
            return
        conn.execute("ALTER TABLE users ADD COLUMN username TEXT")
        conn.execute(
            """
            UPDATE users
            SET username = lower(
              CASE
                WHEN instr(email, '@') > 1 THEN substr(email, 1, instr(email, '@') - 1)
                ELSE email
              END
            )
            WHERE username IS NULL OR username = ''
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username)")

    def _migrate_credentials_to_approval_actions(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(credentials)")}
        if "approval_required_actions" in columns:
            return
        conn.execute("ALTER TABLE credentials ADD COLUMN approval_required_actions TEXT NOT NULL DEFAULT '[]'")

    def _migrate_leases_to_store_ttl(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(leases)")}
        if "ttl_seconds" in columns:
            return
        conn.execute("ALTER TABLE leases ADD COLUMN ttl_seconds INTEGER NOT NULL DEFAULT 0")
        leases = conn.execute("SELECT id, issued_at, expires_at FROM leases").fetchall()
        for row in leases:
            issued_at = parse_dt(row["issued_at"])
            expires_at = parse_dt(row["expires_at"])
            ttl_seconds = 0
            if issued_at is not None and expires_at is not None:
                ttl_seconds = max(int((expires_at - issued_at).total_seconds()), 0)
            conn.execute("UPDATE leases SET ttl_seconds = ? WHERE id = ?", (ttl_seconds, row["id"]))

    def _migrate_schedules_to_catch_up_policy(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(schedules)")}
        if "catch_up_policy" not in columns:
            conn.execute("ALTER TABLE schedules ADD COLUMN catch_up_policy TEXT NOT NULL DEFAULT 'run_once'")
        conn.execute(
            """
            UPDATE schedules
            SET catch_up_policy = 'run_once'
            WHERE catch_up_policy IS NULL OR catch_up_policy = ''
            """
        )

    def is_setup_complete(self) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def setup_admin(self, username: str, password: str) -> dict[str, Any]:
        normalized_username = username.strip().lower()
        if len(normalized_username) < 3:
            raise StoreError("username must be at least 3 characters")
        if len(password) < 12:
            raise StoreError("admin password must be at least 12 characters")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None:
                raise StoreError("setup is already complete")
            user = {
                "id": f"user_{secrets.token_urlsafe(10)}",
                "username": normalized_username,
                "password_hash": hash_password(password),
                "role": "admin",
                "created_at": iso(),
            }
            conn.execute(
                "INSERT INTO users (id, username, password_hash, role, created_at) VALUES (:id, :username, :password_hash, :role, :created_at)",
                user,
            )
        self.seed_demo_if_empty()
        return self.public_user(user)

    def login(self, username: str, password: str) -> tuple[str, dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip().lower(),)).fetchone()
            if row is None or not verify_password(password, row["password_hash"]):
                raise StoreError("invalid username or password")
            token = secrets.token_urlsafe(32)
            token_hash = self.hash_token(token)
            now = utcnow()
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token_hash, row["id"], iso(now), iso(now + timedelta(hours=12))),
            )
            return token, self.public_user(row)

    def get_session_user(self, token: str | None) -> SessionUser | None:
        if not token:
            return None
        token_hash = self.hash_token(token)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT users.id, users.username, users.role, sessions.expires_at
                FROM sessions JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if parse_dt(row["expires_at"]) <= utcnow():
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
                return None
            return SessionUser(id=row["id"], username=row["username"], role=row["role"])

    def logout(self, token: str | None) -> None:
        if not token:
            return
        token_hash = self.hash_token(token)
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def public_user(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {"id": row["id"], "username": row["username"], "role": row["role"], "created_at": row["created_at"]}

    def seed_demo_if_empty(self) -> None:
        with self.connect() as conn:
            if conn.execute("SELECT 1 FROM agents LIMIT 1").fetchone() is not None:
                return
            now = iso()
            agent_id = f"agent_{secrets.token_urlsafe(8)}"
            conn.execute(
                """
                INSERT INTO agents (id, name, role, provider, model, status, system_prompt, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    "Scout",
                    "Gather concise context and report actionable findings.",
                    "local",
                    "deterministic-policy",
                    "idle",
                    "Communicate in short, actionable updates.",
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO credentials
                (id, name, provider, secret_ref, allowed_agents, allowed_actions, approval_required_actions, max_ttl_seconds, require_intent, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "cred_demo_github",
                    "Demo GitHub Capability",
                    "github",
                    "env://HIVEMIND_DEMO_GITHUB_TOKEN",
                    dumps([agent_id]),
                    dumps(["open_issue", "read_repo"]),
                    dumps([]),
                    120,
                    1,
                    dumps({"purpose": "safe local demo credential reference"}),
                    now,
                    now,
                ),
            )

    def list_agents(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM agents ORDER BY created_at DESC")]

    def create_agent(self, data: dict[str, Any]) -> dict[str, Any]:
        now = iso()
        row = {
            "id": f"agent_{secrets.token_urlsafe(8)}",
            "name": data["name"],
            "role": data["role"],
            "provider": data.get("provider") or "local",
            "model": data.get("model") or "deterministic-policy",
            "status": "idle",
            "system_prompt": data.get("system_prompt") or "",
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO agents (id, name, role, provider, model, status, system_prompt, created_at, updated_at)
                VALUES (:id, :name, :role, :provider, :model, :status, :system_prompt, :created_at, :updated_at)
                """,
                row,
            )
        return row

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
            if row is None:
                raise StoreNotFoundError(f"unknown agent: {agent_id}")
            return dict(row)

    def list_credentials(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [self.public_credential(row) for row in conn.execute("SELECT * FROM credentials ORDER BY created_at DESC")]

    def require_guided_credential_fields(
        self,
        *,
        kind: str,
        provider: str,
        metadata: dict[str, Any],
        fields: tuple[str, ...],
    ) -> None:
        if provider != "github":
            raise StoreError(f"{kind} credentials must use provider github")
        for field in fields:
            if not metadata.get(field):
                raise StoreError(f"{kind} metadata requires {field}")

    def normalize_credential_metadata(self, provider: str, metadata: dict[str, Any] | None) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for key, value in (metadata or {}).items():
            if value is None:
                continue
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    continue
            normalized[key] = value
        kind = normalized.get("credential_kind")
        if kind is None:
            return normalized
        kind = str(kind).strip().lower()
        normalized["credential_kind"] = kind
        if kind not in {"generic_reference", "github_oauth_app", "github_app"}:
            raise StoreError(f"unsupported credential_kind: {kind}")
        if kind == "github_oauth_app":
            self.require_guided_credential_fields(
                kind=kind,
                provider=provider,
                metadata=normalized,
                fields=("client_id",),
            )
        elif kind == "github_app":
            self.require_guided_credential_fields(
                kind=kind,
                provider=provider,
                metadata=normalized,
                fields=("app_id", "installation_id"),
            )
        return normalized

    def _prepare_credential_row(self, data: dict[str, Any]) -> dict[str, Any]:
        now = iso()
        actions = sorted(set(action.strip().lower() for action in data["allowed_actions"] if action.strip()))
        agents = sorted(set(agent.strip() for agent in (data.get("allowed_agents") or []) if agent.strip()))
        approval_required_actions = sorted(
            set(action.strip().lower() for action in (data.get("approval_required_actions") or []) if action.strip())
        )
        provider = str(data["provider"]).strip().lower()
        name = str(data["name"]).strip()
        secret_ref = str(data["secret_ref"]).strip()
        metadata = self.normalize_credential_metadata(provider, data.get("metadata"))
        if not actions:
            raise StoreError("credential must allow at least one action")
        if not set(approval_required_actions).issubset(actions):
            raise StoreError("approval_required_actions must be a subset of allowed_actions")
        if not name:
            raise StoreError("credential name is required")
        if not provider:
            raise StoreError("provider is required")
        row = {
            "id": data.get("id") or f"cred_{secrets.token_urlsafe(8)}",
            "name": name,
            "provider": provider,
            "secret_ref": secret_ref,
            "allowed_agents": dumps(agents),
            "allowed_actions": dumps(actions),
            "approval_required_actions": dumps(approval_required_actions),
            "max_ttl_seconds": int(data.get("max_ttl_seconds") or 300),
            "require_intent": 1 if data.get("require_intent", True) else 0,
            "metadata": dumps(metadata),
            "created_at": now,
            "updated_at": now,
        }
        try:
            row["secret_ref"] = validate_secret_ref(row["secret_ref"])
        except ValueError as exc:
            raise StoreError(str(exc)) from exc
        return row

    def create_credential(self, data: dict[str, Any]) -> dict[str, Any]:
        row = self._prepare_credential_row(data)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO credentials
                (id, name, provider, secret_ref, allowed_agents, allowed_actions, approval_required_actions, max_ttl_seconds, require_intent, metadata, created_at, updated_at)
                VALUES (:id, :name, :provider, :secret_ref, :allowed_agents, :allowed_actions, :approval_required_actions, :max_ttl_seconds, :require_intent, :metadata, :created_at, :updated_at)
                """,
                row,
            )
        return self.public_credential(row)

    def create_oauth_state(
        self,
        *,
        user_id: str,
        provider: str,
        pkce_verifier: str,
        credential_payload: dict[str, Any],
    ) -> str:
        now = utcnow()
        row = {
            "id": f"oauth_state_{secrets.token_urlsafe(18)}",
            "user_id": user_id,
            "provider": provider,
            "pkce_verifier": pkce_verifier,
            "credential_payload": dumps(credential_payload),
            "created_at": iso(now),
            "expires_at": iso(now + timedelta(minutes=10)),
        }
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO oauth_states (id, user_id, provider, pkce_verifier, credential_payload, created_at, expires_at)
                VALUES (:id, :user_id, :provider, :pkce_verifier, :credential_payload, :created_at, :expires_at)
                """,
                row,
            )
        return row["id"]

    def consume_oauth_state(self, *, state_id: str, provider: str, user_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM oauth_states WHERE id = ? AND provider = ? AND user_id = ?",
                (state_id, provider, user_id),
            ).fetchone()
            if row is None:
                raise StoreNotFoundError("unknown oauth state")
            conn.execute("DELETE FROM oauth_states WHERE id = ?", (state_id,))
        if parse_dt(row["expires_at"]) <= utcnow():
            raise StoreError("oauth state is expired")
        return {
            "id": row["id"],
            "provider": row["provider"],
            "pkce_verifier": row["pkce_verifier"],
            "credential_payload": loads(row["credential_payload"], {}),
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }

    def create_oauth_credential(
        self,
        *,
        provider: str,
        token_payload: Any,
        requested_credential: dict[str, Any],
        secret_box: SecretBox,
        actor_id: str,
    ) -> dict[str, Any]:
        if not isinstance(token_payload, Mapping):
            raise StoreError("oauth token response must be a JSON object")
        token_payload = dict(token_payload)
        access_token = token_payload.get("access_token")
        if not access_token:
            raise StoreError("oauth token response did not include access_token")
        now = utcnow()
        expires_in = token_payload.get("expires_in")
        token_expires_at = None
        if expires_in not in (None, ""):
            token_expires_at = iso(now + timedelta(seconds=int(expires_in)))
        scope_values = tuple(part for part in str(token_payload.get("scope") or "").split() if part)
        scopes = sorted(set(scope_values))
        metadata = {
            **(requested_credential.get("metadata") or {}),
            "auth_type": "oauth",
            "oauth_provider": provider,
            "oauth_scopes": scopes,
            "oauth_refreshable": bool(token_payload.get("refresh_token")),
            "oauth_connected_at": iso(now),
            "oauth_token_expires_at": token_expires_at,
        }
        credential_id = f"cred_{secrets.token_urlsafe(8)}"
        credential_row = self._prepare_credential_row(
            {
                **requested_credential,
                "id": credential_id,
                "provider": provider,
                "secret_ref": f"oauth://{provider}/{credential_id}",
                "metadata": metadata,
            }
        )
        oauth_row = {
            "credential_id": credential_id,
            "provider": provider,
            "scopes": dumps(scopes),
            "token_ciphertext": secret_box.encrypt_json(token_payload),
            "token_expires_at": token_expires_at,
            "has_refresh_token": 1 if token_payload.get("refresh_token") else 0,
            "created_at": credential_row["created_at"],
            "updated_at": credential_row["updated_at"],
        }
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO credentials
                (id, name, provider, secret_ref, allowed_agents, allowed_actions, approval_required_actions, max_ttl_seconds, require_intent, metadata, created_at, updated_at)
                VALUES (:id, :name, :provider, :secret_ref, :allowed_agents, :allowed_actions, :approval_required_actions, :max_ttl_seconds, :require_intent, :metadata, :created_at, :updated_at)
                """,
                credential_row,
            )
            conn.execute(
                """
                INSERT INTO oauth_connections
                (credential_id, provider, scopes, token_ciphertext, token_expires_at, has_refresh_token, created_at, updated_at)
                VALUES (:credential_id, :provider, :scopes, :token_ciphertext, :token_expires_at, :has_refresh_token, :created_at, :updated_at)
                """,
                oauth_row,
            )
        self.audit(
            "credential.oauth.connected",
            actor_id,
            credential_id,
            "allowed",
            "oauth credential connected",
            {
                "provider": provider,
                "scopes": scopes,
                "refreshable": bool(token_payload.get("refresh_token")),
            },
        )
        return self.public_credential(credential_row)

    def get_credential(self, credential_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM credentials WHERE id = ?", (credential_id,)).fetchone()
            if row is None:
                raise StoreNotFoundError(f"unknown credential: {credential_id}")
            return dict(row)

    def public_credential(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "provider": row["provider"],
            "secret_ref_preview": preview_secret_ref(row["secret_ref"]),
            "policy": {
                "allowed_agents": loads(row["allowed_agents"], []),
                "allowed_actions": loads(row["allowed_actions"], []),
                "approval_required_actions": loads(row["approval_required_actions"], []),
                "max_ttl_seconds": row["max_ttl_seconds"],
                "require_intent": bool(row["require_intent"]),
            },
            "metadata": loads(row["metadata"], {}),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def request_lease(self, credential_id: str, agent_id: str, action: str, intent: str, ttl_seconds: int | None) -> tuple[str | None, dict[str, Any]]:
        self.get_agent(agent_id)
        credential = self.get_credential(credential_id)
        approval_required_actions = set(loads(credential["approval_required_actions"], []))
        review = self._policy_engine.review_request(
            PolicyReviewInput(
                credential_id=credential_id,
                credential_provider=credential["provider"],
                allowed_agents=frozenset(loads(credential["allowed_agents"], [])),
                allowed_actions=frozenset(loads(credential["allowed_actions"], [])),
                require_intent=bool(credential["require_intent"]),
                agent_id=agent_id,
                action=action,
                intent=intent,
                credential_metadata=loads(credential["metadata"], {}),
            )
        )
        normalized_action = review.normalized_action
        if not review.allowed:
            self.audit(LEASE_DENIED_EVENT, agent_id, credential_id, "denied", review.reason, {"action": normalized_action})
            raise StoreError(review.reason)
        ttl = min(int(ttl_seconds or credential["max_ttl_seconds"]), int(credential["max_ttl_seconds"]))
        requires_approval = normalized_action in approval_required_actions
        token = f"hvp_{secrets.token_urlsafe(24)}" if requires_approval else f"hvl_{secrets.token_urlsafe(24)}"
        now = utcnow()
        row = {
            "id": f"lease_{secrets.token_urlsafe(12)}",
            "token_hash": self.hash_token(token),
            "token_preview": "not issued" if requires_approval else f"{token[:8]}...",
            "credential_id": credential_id,
            "agent_id": agent_id,
            "action": normalized_action,
            "intent": intent,
            "ttl_seconds": ttl,
            "status": "pending" if requires_approval else "active",
            "issued_at": iso(now),
            "expires_at": iso(now + timedelta(seconds=ttl)),
        }
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO leases (id, token_hash, token_preview, credential_id, agent_id, action, intent, ttl_seconds, status, issued_at, expires_at)
                VALUES (:id, :token_hash, :token_preview, :credential_id, :agent_id, :action, :intent, :ttl_seconds, :status, :issued_at, :expires_at)
                """,
                row,
            )
        if requires_approval:
            self.audit(
                "credential.lease.pending",
                agent_id,
                credential_id,
                "pending",
                "action requires operator approval",
                {"action": normalized_action, "lease_id": row["id"], "ttl_seconds": ttl},
            )
            return None, self.public_lease(row)
        self.audit("credential.lease.issued", agent_id, credential_id, "allowed", review.reason, {"action": normalized_action, "lease_id": row["id"], "ttl_seconds": ttl})
        public = self.public_lease(row)
        public["lease_token"] = token
        return token, public

    def perform_credential_action(self, lease_token: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        token_hash = self.hash_token(lease_token)
        normalized_action = action.strip().lower()
        error_detail: str | None = None
        result: dict[str, Any] | None = None
        with self.connect() as conn:
            lease = conn.execute("SELECT * FROM leases WHERE token_hash = ?", (token_hash,)).fetchone()
            if lease is None:
                error_detail = "unknown credential lease token"
                self._insert_unknown_credential_action_denial(conn, normalized_action, error_detail)
            else:
                error_detail = self._credential_action_denial_reason(lease, normalized_action)
                if error_detail is not None:
                    self._insert_credential_action_denial(conn, lease, normalized_action, error_detail)
                else:
                    result, error_detail = self._consume_credential_action(conn, lease, normalized_action, payload)
        if error_detail is not None:
            raise StoreError(error_detail)
        if result is None:
            raise RuntimeError("credential action flow ended without a result")
        return result

    def _credential_action_denial_reason(self, lease: sqlite3.Row, normalized_action: str) -> str | None:
        if lease["status"] == "pending":
            return "credential lease is pending approval"
        if lease["status"] == "denied":
            return "credential lease request was denied"
        if lease["status"] != "active" or parse_dt(lease["expires_at"]) <= utcnow():
            return "credential lease is expired or revoked"
        if lease["action"] != normalized_action:
            return "credential lease does not allow this action"
        return None

    def _insert_unknown_credential_action_denial(
        self,
        conn: sqlite3.Connection,
        normalized_action: str,
        error_detail: str,
    ) -> None:
        self._insert_audit(
            conn,
            ACTION_DENIED_EVENT,
            "unknown",
            "credential_lease",
            "denied",
            error_detail,
            {"action": normalized_action},
        )

    def _insert_credential_action_denial(
        self,
        conn: sqlite3.Connection,
        lease: sqlite3.Row,
        normalized_action: str,
        error_detail: str,
    ) -> None:
        self._insert_audit(
            conn,
            ACTION_DENIED_EVENT,
            lease["agent_id"],
            lease["credential_id"],
            "denied",
            error_detail,
            {"action": normalized_action},
        )

    def _consume_credential_action(
        self,
        conn: sqlite3.Connection,
        lease: sqlite3.Row,
        normalized_action: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        credential = conn.execute("SELECT * FROM credentials WHERE id = ?", (lease["credential_id"],)).fetchone()
        if credential is None:
            error_detail = "credential no longer exists"
            self._insert_credential_action_denial(conn, lease, normalized_action, error_detail)
            return None, error_detail
        consumed_at = utcnow()
        cursor = conn.execute(
            """
            UPDATE leases
            SET status = ?, expires_at = ?
            WHERE id = ?
              AND status = ?
              AND action = ?
              AND expires_at > ?
            """,
            ("revoked", iso(consumed_at), lease["id"], "active", normalized_action, iso(consumed_at)),
        )
        if cursor.rowcount != 1:
            error_detail = "credential lease is expired or revoked"
            self._insert_credential_action_denial(conn, lease, normalized_action, error_detail)
            return None, error_detail
        self._insert_audit(
            conn,
            "credential.action.performed",
            lease["agent_id"],
            lease["credential_id"],
            "allowed",
            "action matched active credential lease",
            {"action": normalized_action, "payload_keys": sorted(payload.keys())},
        )
        return (
            {
                "ok": True,
                "provider": credential["provider"],
                "credential_id": credential["id"],
                "action": normalized_action,
                "result": "credential lease matched requested action",
            },
            None,
        )

    def approve_lease(self, lease_id: str, actor_id: str) -> tuple[str, dict[str, Any]]:
        with self.connect() as conn:
            lease = conn.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
            if lease is None:
                raise StoreNotFoundError(f"unknown lease: {lease_id}")
            if lease["status"] != "pending":
                raise StoreError("credential lease is not pending approval")
            token = f"hvl_{secrets.token_urlsafe(24)}"
            now = utcnow()
            expires_at = now + timedelta(seconds=int(lease["ttl_seconds"]))
            conn.execute(
                """
                UPDATE leases
                SET token_hash = ?, token_preview = ?, status = ?, issued_at = ?, expires_at = ?
                WHERE id = ?
                """,
                (self.hash_token(token), f"{token[:8]}...", "active", iso(now), iso(expires_at), lease_id),
            )
            updated = dict(lease)
            updated["token_hash"] = self.hash_token(token)
            updated["token_preview"] = f"{token[:8]}..."
            updated["status"] = "active"
            updated["issued_at"] = iso(now)
            updated["expires_at"] = iso(expires_at)
        self.audit(
            "credential.lease.approved",
            actor_id,
            updated["credential_id"],
            "allowed",
            "operator approved lease request",
            {
                "action": updated["action"],
                "agent_id": updated["agent_id"],
                "lease_id": updated["id"],
                "ttl_seconds": updated["ttl_seconds"],
            },
        )
        public = self.public_lease(updated)
        public["lease_token"] = token
        return token, public

    def deny_lease(self, lease_id: str, actor_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            lease = conn.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
            if lease is None:
                raise StoreNotFoundError(f"unknown lease: {lease_id}")
            if lease["status"] != "pending":
                raise StoreError("credential lease is not pending approval")
            conn.execute("UPDATE leases SET status = ? WHERE id = ?", ("denied", lease_id))
            updated = dict(lease)
            updated["status"] = "denied"
        self.audit(
            LEASE_DENIED_EVENT,
            actor_id,
            updated["credential_id"],
            "denied",
            "operator denied lease request",
            {
                "action": updated["action"],
                "agent_id": updated["agent_id"],
                "lease_id": updated["id"],
                "ttl_seconds": updated["ttl_seconds"],
            },
        )
        return self.public_lease(updated)

    def hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def list_leases(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [self.public_lease(row) for row in conn.execute("SELECT * FROM leases ORDER BY issued_at DESC")]

    def public_lease(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        status = row["status"]
        if status == "active" and parse_dt(row["expires_at"]) <= utcnow():
            status = "expired"
        return {
            "id": row["id"],
            "credential_id": row["credential_id"],
            "agent_id": row["agent_id"],
            "action": row["action"],
            "intent": row["intent"],
            "issued_at": row["issued_at"],
            "expires_at": row["expires_at"],
            "ttl_seconds": row["ttl_seconds"],
            "status": status,
            "token_preview": row["token_preview"] if status not in {"pending", "denied"} else "not issued",
        }

    def get_task_row(self, conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = conn.execute(TASK_BY_ID_QUERY, (task_id,)).fetchone()
        if row is None:
            raise StoreNotFoundError(f"unknown task: {task_id}")
        return row

    def get_schedule_row(self, conn: sqlite3.Connection, schedule_id: str) -> sqlite3.Row:
        row = conn.execute(SCHEDULE_BY_ID_QUERY, (schedule_id,)).fetchone()
        if row is None:
            raise StoreNotFoundError(f"unknown schedule: {schedule_id}")
        return row

    def validate_optional_agent_reference(
        self,
        conn: sqlite3.Connection,
        *,
        field_name: str,
        value: str | None,
    ) -> None:
        if value is None:
            return
        row = conn.execute("SELECT 1 FROM agents WHERE id = ?", (value,)).fetchone()
        if row is None:
            raise StoreValidationError(f"{field_name} references unknown agent: {value}")

    def validate_optional_credential_reference(
        self,
        conn: sqlite3.Connection,
        *,
        field_name: str,
        value: str | None,
    ) -> None:
        if value is None:
            return
        row = conn.execute("SELECT 1 FROM credentials WHERE id = ?", (value,)).fetchone()
        if row is None:
            raise StoreValidationError(f"{field_name} references unknown credential: {value}")

    def create_task(self, data: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        heartbeat_seconds = data.get("heartbeat_seconds")
        row = {
            "id": f"task_{secrets.token_urlsafe(10)}",
            "title": data["title"],
            "description": data.get("description") or "",
            "status": data.get("status") or "queued",
            "priority": data.get("priority") or "normal",
            "assigned_agent_id": data.get("assigned_agent_id") or None,
            "credential_id": data.get("credential_id") or None,
            "action": data.get("action") or "",
            "intent": data.get("intent") or "",
            "heartbeat_seconds": heartbeat_seconds,
            "next_heartbeat_at": iso(now + timedelta(seconds=int(heartbeat_seconds))) if heartbeat_seconds else None,
            "created_at": iso(now),
            "updated_at": iso(now),
        }
        with self.connect() as conn:
            self.validate_optional_agent_reference(
                conn,
                field_name="assigned_agent_id",
                value=row["assigned_agent_id"],
            )
            self.validate_optional_credential_reference(
                conn,
                field_name="credential_id",
                value=row["credential_id"],
            )
            try:
                conn.execute(
                    """
                    INSERT INTO tasks
                    (id, title, description, status, priority, assigned_agent_id, credential_id, action, intent, heartbeat_seconds, next_heartbeat_at, created_at, updated_at)
                    VALUES (:id, :title, :description, :status, :priority, :assigned_agent_id, :credential_id, :action, :intent, :heartbeat_seconds, :next_heartbeat_at, :created_at, :updated_at)
                    """,
                    row,
                )
            except sqlite3.IntegrityError as exc:
                raise StoreValidationError("task references an unknown agent or credential") from exc
        self.audit("task.created", row["assigned_agent_id"] or "user", row["id"], "allowed", "task created", {"status": row["status"]})
        return row

    def list_tasks(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY created_at DESC")]

    def update_task_status(self, task_id: str, status: str) -> dict[str, Any]:
        now = iso()
        with self.connect() as conn:
            row = self.get_task_row(conn, task_id)
            conn.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", (status, now, task_id))
        self.audit("task.status.updated", row["assigned_agent_id"] or "user", task_id, "allowed", f"task marked {status}", {})
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            return dict(self.get_task_row(conn, task_id))

    def record_heartbeat(self, task_id: str, agent_id: str | None, note: str) -> dict[str, Any]:
        now = utcnow()
        with self.connect() as conn:
            task = self.get_task_row(conn, task_id)
            provided_agent_id = agent_id or None
            self.validate_optional_agent_reference(
                conn,
                field_name="agent_id",
                value=provided_agent_id,
            )
            next_heartbeat = None
            if task["heartbeat_seconds"]:
                next_heartbeat = iso(now + timedelta(seconds=int(task["heartbeat_seconds"])))
            event = {
                "id": f"hb_{secrets.token_urlsafe(10)}",
                "task_id": task_id,
                "agent_id": provided_agent_id or task["assigned_agent_id"],
                "note": note,
                "created_at": iso(now),
            }
            try:
                conn.execute(
                    "INSERT INTO heartbeat_events (id, task_id, agent_id, note, created_at) VALUES (:id, :task_id, :agent_id, :note, :created_at)",
                    event,
                )
            except sqlite3.IntegrityError as exc:
                raise StoreValidationError("agent_id references unknown agent") from exc
            conn.execute("UPDATE tasks SET next_heartbeat_at = ?, updated_at = ? WHERE id = ?", (next_heartbeat, iso(now), task_id))
        self.audit("task.heartbeat", event["agent_id"] or "user", task_id, "allowed", "heartbeat recorded", {"note": note})
        return event

    def list_heartbeats(self, task_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if task_id:
                rows = conn.execute("SELECT * FROM heartbeat_events WHERE task_id = ? ORDER BY created_at DESC", (task_id,))
            else:
                rows = conn.execute("SELECT * FROM heartbeat_events ORDER BY created_at DESC")
            return [dict(row) for row in rows]

    def create_schedule(self, data: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        interval = int(data["interval_seconds"])
        if interval < 60:
            raise StoreError("schedule interval must be at least 60 seconds")
        catch_up_policy = data.get("catch_up_policy") or "run_once"
        if catch_up_policy not in SCHEDULE_CATCH_UP_POLICIES:
            raise StoreError(f"unsupported catch-up policy: {catch_up_policy}")
        next_run_at = (
            require_aware_utc(data["next_run_at"], field_name="next_run_at")
            if data.get("next_run_at")
            else now + timedelta(seconds=interval)
        )
        row = {
            "id": f"sched_{secrets.token_urlsafe(10)}",
            "name": data["name"],
            "enabled": 1 if data.get("enabled", True) else 0,
            "interval_seconds": interval,
            "catch_up_policy": catch_up_policy,
            "task_title": data["task_title"],
            "task_description": data.get("task_description") or "",
            "priority": data.get("priority") or "normal",
            "assigned_agent_id": data.get("assigned_agent_id") or None,
            "credential_id": data.get("credential_id") or None,
            "action": data.get("action") or "",
            "intent": data.get("intent") or "",
            "next_run_at": iso(next_run_at),
            "last_run_at": None,
            "created_at": iso(now),
            "updated_at": iso(now),
        }
        with self.connect() as conn:
            self.validate_optional_agent_reference(
                conn,
                field_name="assigned_agent_id",
                value=row["assigned_agent_id"],
            )
            self.validate_optional_credential_reference(
                conn,
                field_name="credential_id",
                value=row["credential_id"],
            )
            try:
                conn.execute(
                    """
                    INSERT INTO schedules
                    (id, name, enabled, interval_seconds, catch_up_policy, task_title, task_description, priority, assigned_agent_id, credential_id, action, intent, next_run_at, last_run_at, created_at, updated_at)
                    VALUES (:id, :name, :enabled, :interval_seconds, :catch_up_policy, :task_title, :task_description, :priority, :assigned_agent_id, :credential_id, :action, :intent, :next_run_at, :last_run_at, :created_at, :updated_at)
                    """,
                    row,
                )
            except sqlite3.IntegrityError as exc:
                raise StoreValidationError("schedule references an unknown agent or credential") from exc
        self.audit(
            "schedule.created",
            row["assigned_agent_id"] or "user",
            row["id"],
            "allowed",
            "schedule created",
            {"interval_seconds": interval, "catch_up_policy": catch_up_policy},
        )
        return self.public_schedule(row)

    def list_schedules(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [self.public_schedule(row) for row in conn.execute("SELECT * FROM schedules ORDER BY created_at DESC")]

    def update_schedule_enabled(self, schedule_id: str, enabled: bool) -> dict[str, Any]:
        now = iso()
        with self.connect() as conn:
            row = self.get_schedule_row(conn, schedule_id)
            conn.execute(
                "UPDATE schedules SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, now, schedule_id),
            )
        actor = row["assigned_agent_id"] or "user"
        reason = "schedule enabled" if enabled else "schedule paused"
        self.audit("schedule.enabled.updated", actor, schedule_id, "allowed", reason, {"enabled": enabled})
        return self.get_schedule(schedule_id)

    def run_due_schedules_once(self) -> list[dict[str, Any]]:
        now = utcnow()
        created: list[dict[str, Any]] = []
        with self._lock:
            with self.connect() as conn:
                rows = list(
                    conn.execute(
                        "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at ASC",
                        (iso(now),),
                    )
                )
            for row in rows:
                interval_seconds = int(row["interval_seconds"])
                interval = timedelta(seconds=interval_seconds)
                catch_up_policy = row["catch_up_policy"] or "run_once"
                if catch_up_policy not in SCHEDULE_CATCH_UP_POLICIES:
                    raise StoreError(f"unsupported catch-up policy: {catch_up_policy}")
                next_run_at = require_aware_utc(row["next_run_at"], field_name="next_run_at")
                if next_run_at > now:
                    continue
                missed_run_count = int((now - next_run_at).total_seconds() // interval_seconds) + 1
                if catch_up_policy == "backfill":
                    run_count = min(missed_run_count, SCHEDULE_BACKFILL_BATCH_LIMIT)
                    scheduled_runs = [next_run_at + (interval * index) for index in range(run_count)]
                    next_run = next_run_at + (interval * run_count)
                    skipped_run_count = 0
                    remaining_run_count = missed_run_count - run_count
                elif catch_up_policy == "skip_missed":
                    scheduled_runs = [next_run_at + (interval * (missed_run_count - 1))]
                    next_run = next_run_at + (interval * missed_run_count)
                    skipped_run_count = missed_run_count - 1
                    remaining_run_count = 0
                else:
                    scheduled_runs = [now]
                    next_run = now + interval
                    skipped_run_count = missed_run_count - 1
                    remaining_run_count = 0

                task_ids: list[str] = []
                for _ in scheduled_runs:
                    task = self.create_task(
                        {
                            "title": row["task_title"],
                            "description": row["task_description"],
                            "priority": row["priority"],
                            "assigned_agent_id": row["assigned_agent_id"],
                            "credential_id": row["credential_id"],
                            "action": row["action"],
                            "intent": row["intent"],
                            "heartbeat_seconds": None,
                        }
                    )
                    created.append(task)
                    task_ids.append(task["id"])
                with self.connect() as conn:
                    conn.execute(
                        "UPDATE schedules SET last_run_at = ?, next_run_at = ?, updated_at = ? WHERE id = ?",
                        (iso(now), iso(next_run), iso(now), row["id"]),
                    )
                self.audit(
                    "schedule.ran",
                    row["assigned_agent_id"] or "scheduler",
                    row["id"],
                    "allowed",
                    "scheduled task created",
                    {
                        "catch_up_policy": catch_up_policy,
                        "created_task_count": len(task_ids),
                        "missed_run_count": missed_run_count,
                        "remaining_run_count": remaining_run_count,
                        "scheduled_for": [iso(scheduled_for) for scheduled_for in scheduled_runs],
                        "skipped_run_count": skipped_run_count,
                        "task_ids": task_ids,
                    },
                )
        return created

    def get_schedule(self, schedule_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            return self.public_schedule(self.get_schedule_row(conn, schedule_id))

    def public_schedule(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        return item

    def audit(self, event_type: str, actor_id: str, target_id: str, decision: str, reason: str, metadata: dict[str, Any]) -> None:
        with self.connect() as conn:
            self._insert_audit(conn, event_type, actor_id, target_id, decision, reason, metadata)

    def list_audit_events(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                {**dict(row), "metadata": loads(row["metadata"], {})}
                for row in conn.execute("SELECT * FROM audit_events ORDER BY created_at DESC LIMIT 200")
            ]

    def _insert_audit(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        actor_id: str,
        target_id: str,
        decision: str,
        reason: str,
        metadata: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO audit_events (id, type, actor_id, target_id, decision, reason, metadata, created_at) VALUES (:id, :type, :actor_id, :target_id, :decision, :reason, :metadata, :created_at)",
            {
                "id": f"audit_{secrets.token_urlsafe(10)}",
                "type": event_type,
                "actor_id": actor_id,
                "target_id": target_id,
                "decision": decision,
                "reason": reason,
                "metadata": dumps(metadata),
                "created_at": iso(),
            },
        )
