from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterator
from urllib.parse import urlparse

from hivemind.config import HivemindConfig
from hivemind.oauth import SecretBox
from hivemind.policy import PolicyEngine, PolicyReviewInput, ProviderIntentReviewer
from hivemind.providers import (
    AgentProviderAdapter,
    AgentProviderError,
    AgentProviderRegistry,
    CREDENTIAL_OPTIONAL_AGENT_PROVIDERS,
    ProviderMessage,
    ProviderRunRequest,
    ProviderToolRequest,
    normalize_agent_provider_id,
)
from hivemind.secret_refs import (
    ALLOWED_SECRET_REF_SCHEMES,
    preview_secret_ref,
    validate_external_credential_metadata,
    validate_external_secret_ref,
    validate_secret_ref,
)

SCHEDULE_BACKFILL_BATCH_LIMIT = 100
SCHEDULE_CATCH_UP_POLICIES = ("skip_missed", "run_once", "backfill")
HIVE_TRACKER_PROVIDERS = ("github", "jira", "linear", "custom")
HIVE_STATUSES = ("active", "paused")
ISSUE_KINDS = ("issue", "feature_request", "bug", "chore")
ISSUE_ACTION_BY_KIND = {
    "issue": "open_issue",
    "feature_request": "open_feature_request",
    "bug": "open_issue",
    "chore": "open_issue",
}


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
ISSUE_REQUEST_DENIED_EVENT = "issue.request.denied"
AGENT_PROVIDER_FAILED_CLOSED_REASON = "agent provider failed closed"
AGENT_PROVIDER_CREDENTIAL_ACTION_PREFIX = "agent_provider:"
REDACTED_VALUE = "[redacted]"
TASK_BY_ID_QUERY = "SELECT * FROM tasks WHERE id = ?"
TASK_STATUS_UPDATE_SQL = "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?"
TASK_RUN_CLAIM_SQL = "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND status = ?"
SCHEDULE_BY_ID_QUERY = "SELECT * FROM schedules WHERE id = ?"
BEGIN_IMMEDIATE_SQL = "BEGIN IMMEDIATE"
BROKER_SECRET_SCHEME = ALLOWED_SECRET_REF_SCHEMES[-1]
CREDENTIAL_INSERT_SQL = """
    INSERT INTO credentials
    (id, name, provider, secret_ref, allowed_agents, allowed_actions, approval_required_actions, max_ttl_seconds, require_intent, metadata, created_at, updated_at)
    VALUES (:id, :name, :provider, :secret_ref, :allowed_agents, :allowed_actions, :approval_required_actions, :max_ttl_seconds, :require_intent, :metadata, :created_at, :updated_at)
"""
BACKUP_FORMAT = "hivemind-logical-backup"
BACKUP_FORMAT_VERSION = 1
BACKUP_TABLE_QUERIES: dict[str, str] = {
    "users": "SELECT id, username, password_hash, role, created_at FROM users ORDER BY id",
    "agents": "SELECT * FROM agents ORDER BY id",
    "credentials": (
        "SELECT * FROM credentials "
        "WHERE secret_ref NOT LIKE 'oauth://%' AND secret_ref NOT LIKE 'secret://%' "
        "ORDER BY id"
    ),
    "tasks": "SELECT * FROM tasks ORDER BY id",
    "schedules": "SELECT * FROM schedules ORDER BY id",
    "heartbeat_events": "SELECT * FROM heartbeat_events ORDER BY id",
    "audit_events": "SELECT * FROM audit_events ORDER BY id",
}
BACKUP_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "users": ("id", "username", "password_hash", "role", "created_at"),
    "agents": ("id", "name", "role", "provider", "model", "status", "system_prompt", "created_at", "updated_at"),
    "credentials": (
        "id",
        "name",
        "provider",
        "secret_ref",
        "allowed_agents",
        "allowed_actions",
        "approval_required_actions",
        "max_ttl_seconds",
        "require_intent",
        "metadata",
        "created_at",
        "updated_at",
    ),
    "tasks": (
        "id",
        "title",
        "description",
        "status",
        "priority",
        "assigned_agent_id",
        "credential_id",
        "action",
        "intent",
        "heartbeat_seconds",
        "next_heartbeat_at",
        "created_at",
        "updated_at",
    ),
    "schedules": (
        "id",
        "name",
        "enabled",
        "interval_seconds",
        "catch_up_policy",
        "task_title",
        "task_description",
        "priority",
        "assigned_agent_id",
        "credential_id",
        "action",
        "intent",
        "next_run_at",
        "last_run_at",
        "created_at",
        "updated_at",
    ),
    "heartbeat_events": ("id", "task_id", "agent_id", "note", "created_at"),
    "audit_events": ("id", "type", "actor_id", "target_id", "decision", "reason", "metadata", "created_at"),
}
BACKUP_INSERT_STATEMENTS: dict[str, str] = {
    "users": (
        "INSERT INTO users (id, username, password_hash, role, created_at) "
        "VALUES (:id, :username, :password_hash, :role, :created_at)"
    ),
    "agents": (
        "INSERT INTO agents (id, name, role, provider, model, status, system_prompt, created_at, updated_at) "
        "VALUES (:id, :name, :role, :provider, :model, :status, :system_prompt, :created_at, :updated_at)"
    ),
    "credentials": (
        "INSERT INTO credentials (id, name, provider, secret_ref, allowed_agents, allowed_actions, "
        "approval_required_actions, "
        "max_ttl_seconds, require_intent, metadata, created_at, updated_at) "
        "VALUES (:id, :name, :provider, :secret_ref, :allowed_agents, :allowed_actions, "
        ":approval_required_actions, "
        ":max_ttl_seconds, :require_intent, :metadata, :created_at, :updated_at)"
    ),
    "tasks": (
        "INSERT INTO tasks (id, title, description, status, priority, assigned_agent_id, credential_id, "
        "action, intent, heartbeat_seconds, next_heartbeat_at, created_at, updated_at) "
        "VALUES (:id, :title, :description, :status, :priority, :assigned_agent_id, :credential_id, "
        ":action, :intent, :heartbeat_seconds, :next_heartbeat_at, :created_at, :updated_at)"
    ),
    "schedules": (
        "INSERT INTO schedules (id, name, enabled, interval_seconds, catch_up_policy, task_title, task_description, priority, "
        "assigned_agent_id, credential_id, action, intent, next_run_at, last_run_at, created_at, updated_at) "
        "VALUES (:id, :name, :enabled, :interval_seconds, :catch_up_policy, :task_title, :task_description, :priority, "
        ":assigned_agent_id, :credential_id, :action, :intent, :next_run_at, :last_run_at, :created_at, :updated_at)"
    ),
    "heartbeat_events": (
        "INSERT INTO heartbeat_events (id, task_id, agent_id, note, created_at) "
        "VALUES (:id, :task_id, :agent_id, :note, :created_at)"
    ),
    "audit_events": (
        "INSERT INTO audit_events (id, type, actor_id, target_id, decision, reason, metadata, created_at) "
        "VALUES (:id, :type, :actor_id, :target_id, :decision, :reason, :metadata, :created_at)"
    ),
}
BACKUP_DELETE_STATEMENTS = (
    "DELETE FROM oauth_states",
    "DELETE FROM sessions",
    "DELETE FROM leases",
    "DELETE FROM oauth_connections",
    "DELETE FROM broker_secrets",
    "DELETE FROM heartbeat_events",
    "DELETE FROM schedules",
    "DELETE FROM tasks",
    "DELETE FROM audit_events",
    "DELETE FROM credentials",
    "DELETE FROM agents",
    "DELETE FROM users",
)
BACKUP_CREDENTIAL_REFERENCE_TABLES = ("tasks", "schedules")
SENSITIVE_PROVIDER_RESULT_KEYS = frozenset(
    {
        "accesstoken",
        "apikey",
        "authorization",
        "bearer",
        "clientsecret",
        "credentialref",
        "leasetoken",
        "password",
        "refreshtoken",
        "secret",
        "secretref",
        "secretkey",
        "secretvalue",
        "token",
    }
)
PUBLIC_METADATA_NON_SECRET_KEYS = frozenset({"oauthtokenexpiresat"})
SECRET_REF_TEXT_PATTERN = re.compile(r"\b(?:env|file|vault|oauth|secret)://[^\s\"'<>),\]}]+")


def provider_redaction_values(credential_ref: str | None) -> tuple[str, ...]:
    if not credential_ref:
        return ()
    _, _, target = credential_ref.partition("://")
    values = [credential_ref]
    if target:
        values.append(target)
    return tuple(values)


def normalize_sensitive_provider_key(key: Any) -> str:
    return "".join(char for char in str(key).lower() if char.isalnum())


def is_sensitive_provider_key(key: Any) -> bool:
    normalized = normalize_sensitive_provider_key(key)
    return any(sensitive_key in normalized for sensitive_key in SENSITIVE_PROVIDER_RESULT_KEYS)


def is_sensitive_public_metadata_key(key: Any) -> bool:
    normalized = normalize_sensitive_provider_key(key)
    if normalized in PUBLIC_METADATA_NON_SECRET_KEYS:
        return False
    return is_sensitive_provider_key(key)


def redact_provider_public_value(value: Any, credential_ref: str | None) -> Any:
    redactions = provider_redaction_values(credential_ref)
    if isinstance(value, str):
        redacted = value
        for secret_value in redactions:
            redacted = redacted.replace(secret_value, REDACTED_VALUE)
        return SECRET_REF_TEXT_PATTERN.sub(
            lambda match: preview_secret_ref(validate_secret_ref(match.group(0))) or REDACTED_VALUE,
            redacted,
        )
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [redact_provider_public_value(item, credential_ref) for item in value]
    if isinstance(value, dict):
        return {
            key: REDACTED_VALUE
            if is_sensitive_provider_key(key)
            else redact_provider_public_value(item, credential_ref)
            for key, item in value.items()
        }
    return value


def redact_public_metadata_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return preview_secret_ref(validate_secret_ref(value))
        except ValueError:
            return value
    if isinstance(value, list):
        return [redact_public_metadata_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: REDACTED_VALUE
            if is_sensitive_public_metadata_key(key)
            else redact_public_metadata_value(item)
            for key, item in value.items()
        }
    return value


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
        agent_provider_adapters: Mapping[str, AgentProviderAdapter] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self.config = config or HivemindConfig.from_env()
        self._policy_engine = PolicyEngine(
            self.config.intent_reviewer,
            provider_reviewers=provider_reviewers,
        )
        self._agent_provider_registry = AgentProviderRegistry(agent_provider_adapters)
        self._migrate()

    @classmethod
    def from_env(
        cls,
        *,
        require_existing: bool = False,
        provider_reviewers: Mapping[str, ProviderIntentReviewer] | None = None,
        agent_provider_adapters: Mapping[str, AgentProviderAdapter] | None = None,
    ) -> "HivemindStore":
        config = HivemindConfig.from_env()
        path = os.getenv("HIVEMIND_DB_PATH", "/data/hivemind.db")
        if path == ":memory:":
            if require_existing:
                raise StoreError("cannot back up ephemeral in-memory database")
            return cls(
                path,
                config=config,
                provider_reviewers=provider_reviewers,
                agent_provider_adapters=agent_provider_adapters,
            )
        db_path = Path(path)
        if require_existing:
            if not db_path.exists():
                raise StoreError("configured database does not exist; check HIVEMIND_DB_PATH")
            if not db_path.is_file():
                raise StoreError("configured database path is not a file; check HIVEMIND_DB_PATH")
        return cls(
            db_path,
            config=config,
            provider_reviewers=provider_reviewers,
            agent_provider_adapters=agent_provider_adapters,
        )

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

                CREATE TABLE IF NOT EXISTS hives (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  project_ref TEXT NOT NULL,
                  tracker_provider TEXT NOT NULL,
                  tracker_project TEXT NOT NULL,
                  tracker_base_url TEXT NOT NULL,
                  tracker_credential_id TEXT REFERENCES credentials(id) ON DELETE SET NULL,
                  guidance TEXT NOT NULL,
                  status TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agents (
                  id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  role TEXT NOT NULL,
                  provider TEXT NOT NULL,
                  model TEXT NOT NULL,
                  status TEXT NOT NULL,
                  system_prompt TEXT NOT NULL DEFAULT '',
                  hive_id TEXT REFERENCES hives(id) ON DELETE SET NULL,
                  can_spawn_subagents INTEGER NOT NULL DEFAULT 0,
                  max_subagents INTEGER NOT NULL DEFAULT 0,
                  issue_creation_enabled INTEGER NOT NULL DEFAULT 0,
                  issue_kind TEXT NOT NULL DEFAULT 'issue',
                  issue_rate_limit_per_hour INTEGER NOT NULL DEFAULT 0,
                  issue_labels TEXT NOT NULL DEFAULT '[]',
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

                CREATE TABLE IF NOT EXISTS broker_secrets (
                  credential_id TEXT PRIMARY KEY REFERENCES credentials(id) ON DELETE CASCADE,
                  ciphertext TEXT NOT NULL,
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
                  hive_id TEXT REFERENCES hives(id) ON DELETE SET NULL,
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
                  hive_id TEXT REFERENCES hives(id) ON DELETE SET NULL,
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
            self._migrate_hive_control_tables(conn)
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

    def _migrate_hive_control_tables(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hives (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              project_ref TEXT NOT NULL,
              tracker_provider TEXT NOT NULL,
              tracker_project TEXT NOT NULL,
              tracker_base_url TEXT NOT NULL,
              tracker_credential_id TEXT REFERENCES credentials(id) ON DELETE SET NULL,
              guidance TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        agent_columns = {row["name"] for row in conn.execute("PRAGMA table_info(agents)")}
        agent_additions = {
            "hive_id": "ALTER TABLE agents ADD COLUMN hive_id TEXT REFERENCES hives(id) ON DELETE SET NULL",
            "can_spawn_subagents": "ALTER TABLE agents ADD COLUMN can_spawn_subagents INTEGER NOT NULL DEFAULT 0",
            "max_subagents": "ALTER TABLE agents ADD COLUMN max_subagents INTEGER NOT NULL DEFAULT 0",
            "issue_creation_enabled": "ALTER TABLE agents ADD COLUMN issue_creation_enabled INTEGER NOT NULL DEFAULT 0",
            "issue_kind": "ALTER TABLE agents ADD COLUMN issue_kind TEXT NOT NULL DEFAULT 'issue'",
            "issue_rate_limit_per_hour": "ALTER TABLE agents ADD COLUMN issue_rate_limit_per_hour INTEGER NOT NULL DEFAULT 0",
            "issue_labels": "ALTER TABLE agents ADD COLUMN issue_labels TEXT NOT NULL DEFAULT '[]'",
        }
        for column, statement in agent_additions.items():
            if column not in agent_columns:
                conn.execute(statement)
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "hive_id" not in task_columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN hive_id TEXT REFERENCES hives(id) ON DELETE SET NULL")
        schedule_columns = {row["name"] for row in conn.execute("PRAGMA table_info(schedules)")}
        if "hive_id" not in schedule_columns:
            conn.execute("ALTER TABLE schedules ADD COLUMN hive_id TEXT REFERENCES hives(id) ON DELETE SET NULL")

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

    def export_backup_bundle(self) -> dict[str, Any]:
        with self.connect() as conn:
            # Keep all logical table reads on the same SQLite snapshot.
            conn.execute("BEGIN")
            tables = {
                table: [dict(row) for row in conn.execute(query)]
                for table, query in BACKUP_TABLE_QUERIES.items()
            }
        tables = self.clear_unrestorable_credential_refs(tables)
        return {
            "format": BACKUP_FORMAT,
            "format_version": BACKUP_FORMAT_VERSION,
            "created_at": iso(),
            "excluded": {
                "tables": ["sessions", "leases", "oauth_states", "oauth_connections", "broker_secrets"],
                "credentials": "oauth-backed and broker-managed credentials are excluded and must be reconnected after restore",
            },
            "summary": {table: len(rows) for table, rows in tables.items()},
            "tables": tables,
        }

    def clear_unrestorable_credential_refs(
        self,
        tables: dict[str, list[dict[str, Any]]],
    ) -> dict[str, list[dict[str, Any]]]:
        credential_ids = {row["id"] for row in tables.get("credentials", [])}
        normalized = dict(tables)
        for table in BACKUP_CREDENTIAL_REFERENCE_TABLES:
            normalized[table] = [
                {
                    **row,
                    "credential_id": row["credential_id"] if row.get("credential_id") in credential_ids else None,
                }
                for row in normalized.get(table, [])
            ]
        return normalized

    def validate_backup_credential_row(self, row: dict[str, Any]) -> dict[str, Any]:
        secret_ref = str(row["secret_ref"])
        if secret_ref.startswith("oauth://"):
            raise StoreValidationError("backup bundle cannot restore oauth-backed broker credentials")
        if secret_ref.startswith(f"{BROKER_SECRET_SCHEME}://"):
            raise StoreValidationError("backup bundle cannot restore broker-managed credentials")
        try:
            row["secret_ref"] = validate_external_secret_ref(secret_ref)
            metadata = loads(str(row.get("metadata")), {})
            if not isinstance(metadata, dict):
                raise ValueError("credential metadata must be a JSON object")
            validate_external_credential_metadata(metadata)
        except ValueError as exc:
            raise StoreValidationError(str(exc)) from exc
        return row

    def validate_backup_schedule_row(self, row: dict[str, Any]) -> dict[str, Any]:
        try:
            row["next_run_at"] = iso(require_aware_utc(row["next_run_at"], field_name="next_run_at"))
            if row.get("last_run_at") is not None:
                row["last_run_at"] = iso(require_aware_utc(row["last_run_at"], field_name="last_run_at"))
        except ValueError as exc:
            raise StoreValidationError(str(exc)) from exc
        return row

    def validate_backup_rows(
        self,
        *,
        table: str,
        rows: Any,
        columns: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            raise StoreValidationError(f"backup table {table} must be a JSON array")
        allowed_columns = set(columns)
        normalized_rows: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise StoreValidationError(f"backup table {table} row {index} must be a JSON object")
            row_dict = dict(row)
            row_columns = set(row_dict)
            extra_columns = sorted(row_columns - allowed_columns)
            if extra_columns:
                extras = ", ".join(extra_columns)
                raise StoreValidationError(f"backup table {table} contains unsupported columns: {extras}")
            missing_columns = [column for column in columns if column not in row_dict]
            if missing_columns:
                missing = ", ".join(missing_columns)
                raise StoreValidationError(f"backup table {table} row {index} is missing columns: {missing}")
            if table == "credentials":
                row_dict = self.validate_backup_credential_row(row_dict)
            if table == "schedules":
                row_dict = self.validate_backup_schedule_row(row_dict)
            normalized_rows.append(row_dict)
        return normalized_rows

    def validate_backup_bundle(
        self,
        bundle: Mapping[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        if bundle.get("format") != BACKUP_FORMAT:
            raise StoreValidationError(f"unsupported backup format: {bundle.get('format')!r}")
        if bundle.get("format_version") != BACKUP_FORMAT_VERSION:
            raise StoreValidationError(
                "unsupported backup format version: "
                f"{bundle.get('format_version')!r}; expected {BACKUP_FORMAT_VERSION}"
            )
        tables = bundle.get("tables")
        if not isinstance(tables, Mapping):
            raise StoreValidationError("backup bundle tables must be a JSON object")

        missing_tables = [table for table in BACKUP_TABLE_QUERIES if table not in tables]
        if missing_tables:
            missing = ", ".join(missing_tables)
            raise StoreValidationError(f"backup bundle is missing required tables: {missing}")

        tables = {
            table: self.validate_backup_rows(table=table, rows=tables[table], columns=BACKUP_TABLE_COLUMNS[table])
            for table in BACKUP_TABLE_QUERIES
        }
        return self.clear_unrestorable_credential_refs(tables)

    def restore_backup_bundle(self, bundle: Mapping[str, Any]) -> dict[str, int]:
        with self.connect() as conn:
            conn.execute(BEGIN_IMMEDIATE_SQL)
            tables = self.validate_backup_bundle(bundle)
            for statement in BACKUP_DELETE_STATEMENTS:
                conn.execute(statement)
            for table, rows in tables.items():
                if not rows:
                    continue
                conn.executemany(BACKUP_INSERT_STATEMENTS[table], rows)
        return {table: len(rows) for table, rows in tables.items()}

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
            conn.execute(BEGIN_IMMEDIATE_SQL)
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
            hive_id = "hive_local_runtime"
            agent_id = f"agent_{secrets.token_urlsafe(8)}"
            conn.execute(
                """
                INSERT INTO hives
                (id, name, project_ref, tracker_provider, tracker_project, tracker_base_url, tracker_credential_id, guidance, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    hive_id,
                    "local runtime",
                    "local://hivemind",
                    "github",
                    "hivemind",
                    "",
                    None,
                    "Keep reports brief, cite concrete repo evidence, and queue issue requests through the broker.",
                    "active",
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO agents
                (id, name, role, provider, model, status, system_prompt, hive_id, can_spawn_subagents, max_subagents, issue_creation_enabled, issue_kind, issue_rate_limit_per_hour, issue_labels, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    "Scout",
                    "Gather concise context and report actionable findings.",
                    "local",
                    "deterministic-policy",
                    "idle",
                    "Communicate in short, actionable updates.",
                    hive_id,
                    1,
                    3,
                    1,
                    "issue",
                    4,
                    dumps(["needs-triage"]),
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
            conn.execute(
                "UPDATE hives SET tracker_credential_id = ?, updated_at = ? WHERE id = ?",
                ("cred_demo_github", now, hive_id),
            )

    def normalize_hive_status(self, status: str | None) -> str:
        normalized = str(status or "active").strip().lower()
        if normalized not in HIVE_STATUSES:
            raise StoreError(f"unsupported hive status: {normalized}")
        return normalized

    def normalize_tracker_provider(self, provider: str | None) -> str:
        normalized = str(provider or "github").strip().lower()
        if normalized not in HIVE_TRACKER_PROVIDERS:
            raise StoreError(f"unsupported tracker provider: {normalized}")
        return normalized

    def normalize_tracker_base_url(self, value: str | None) -> str:
        base_url = str(value or "").strip()
        if not base_url:
            return ""
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise StoreError("tracker_base_url must be an http(s) URL")
        if parsed.username or parsed.password:
            raise StoreError("tracker_base_url must not include credentials")
        return base_url

    def normalize_issue_kind(self, value: str | None) -> str:
        kind = str(value or "issue").strip().lower()
        if kind not in ISSUE_KINDS:
            raise StoreError(f"unsupported issue kind: {kind}")
        return kind

    def normalize_labels(self, values: Any) -> list[str]:
        if values is None:
            return []
        if isinstance(values, str):
            values = [item.strip() for item in values.split(",")]
        return sorted({str(value).strip() for value in values if str(value).strip()})

    def list_hives(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                self.public_hive(row, conn)
                for row in conn.execute("SELECT * FROM hives ORDER BY created_at DESC")
            ]

    def create_hive(self, data: dict[str, Any]) -> dict[str, Any]:
        now = iso()
        row = {
            "id": data.get("id") or f"hive_{secrets.token_urlsafe(8)}",
            "name": str(data["name"]).strip(),
            "project_ref": str(data["project_ref"]).strip(),
            "tracker_provider": self.normalize_tracker_provider(data.get("tracker_provider")),
            "tracker_project": str(data.get("tracker_project") or "").strip(),
            "tracker_base_url": self.normalize_tracker_base_url(data.get("tracker_base_url")),
            "tracker_credential_id": data.get("tracker_credential_id") or None,
            "guidance": str(data.get("guidance") or "").strip(),
            "status": self.normalize_hive_status(data.get("status")),
            "created_at": now,
            "updated_at": now,
        }
        if not row["name"]:
            raise StoreError("hive name is required")
        if not row["project_ref"]:
            raise StoreError("hive project_ref is required")
        with self.connect() as conn:
            self.validate_optional_credential_reference(
                conn,
                field_name="tracker_credential_id",
                value=row["tracker_credential_id"],
            )
            conn.execute(
                """
                INSERT INTO hives
                (id, name, project_ref, tracker_provider, tracker_project, tracker_base_url, tracker_credential_id, guidance, status, created_at, updated_at)
                VALUES (:id, :name, :project_ref, :tracker_provider, :tracker_project, :tracker_base_url, :tracker_credential_id, :guidance, :status, :created_at, :updated_at)
                """,
                row,
            )
        self.audit(
            "hive.created",
            "operator",
            row["id"],
            "allowed",
            "hive created",
            {"tracker_provider": row["tracker_provider"], "tracker_project": row["tracker_project"]},
        )
        return self.get_hive(row["id"])

    def update_hive(self, hive_id: str, data: dict[str, Any]) -> dict[str, Any]:
        allowed_fields = {
            "name",
            "project_ref",
            "tracker_provider",
            "tracker_project",
            "tracker_base_url",
            "tracker_credential_id",
            "guidance",
            "status",
        }
        updates: dict[str, Any] = {}
        for field in allowed_fields:
            if field in data:
                updates[field] = data[field]
        if not updates:
            return self.get_hive(hive_id)
        if "tracker_provider" in updates:
            updates["tracker_provider"] = self.normalize_tracker_provider(updates["tracker_provider"])
        if "tracker_base_url" in updates:
            updates["tracker_base_url"] = self.normalize_tracker_base_url(updates["tracker_base_url"])
        if "status" in updates:
            updates["status"] = self.normalize_hive_status(updates["status"])
        for text_field in ("name", "project_ref", "tracker_project", "guidance"):
            if text_field in updates:
                updates[text_field] = str(updates[text_field] or "").strip()
        if "tracker_credential_id" in updates:
            updates["tracker_credential_id"] = updates["tracker_credential_id"] or None
        if updates.get("name") == "":
            raise StoreError("hive name is required")
        if updates.get("project_ref") == "":
            raise StoreError("hive project_ref is required")
        with self.connect() as conn:
            current = dict(self.get_hive_row(conn, hive_id))
            self.validate_optional_credential_reference(
                conn,
                field_name="tracker_credential_id",
                value=updates.get("tracker_credential_id"),
            )
            row = {**current, **updates, "updated_at": iso()}
            conn.execute(
                """
                UPDATE hives
                SET name = :name,
                    project_ref = :project_ref,
                    tracker_provider = :tracker_provider,
                    tracker_project = :tracker_project,
                    tracker_base_url = :tracker_base_url,
                    tracker_credential_id = :tracker_credential_id,
                    guidance = :guidance,
                    status = :status,
                    updated_at = :updated_at
                WHERE id = :id
                """,
                row,
            )
        self.audit(
            "hive.updated",
            "operator",
            hive_id,
            "allowed",
            "hive updated",
            {"fields": sorted(field for field in updates if field != "updated_at")},
        )
        return self.get_hive(hive_id)

    def get_hive(self, hive_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            return self.public_hive(self.get_hive_row(conn, hive_id), conn)

    def get_hive_row(self, conn: sqlite3.Connection, hive_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM hives WHERE id = ?", (hive_id,)).fetchone()
        if row is None:
            raise StoreNotFoundError(f"unknown hive: {hive_id}")
        return row

    def public_hive(self, row: sqlite3.Row | dict[str, Any], conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        item = dict(row)
        if conn is not None:
            item["agent_count"] = conn.execute("SELECT COUNT(*) FROM agents WHERE hive_id = ?", (item["id"],)).fetchone()[0]
            item["issue_agent_count"] = conn.execute(
                "SELECT COUNT(*) FROM agents WHERE hive_id = ? AND issue_creation_enabled = 1",
                (item["id"],),
            ).fetchone()[0]
            item["subagent_enabled_count"] = conn.execute(
                "SELECT COUNT(*) FROM agents WHERE hive_id = ? AND can_spawn_subagents = 1",
                (item["id"],),
            ).fetchone()[0]
            item["open_task_count"] = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE hive_id = ? AND status IN ('queued', 'running', 'blocked')",
                (item["id"],),
            ).fetchone()[0]
            item["schedule_count"] = conn.execute(
                "SELECT COUNT(*) FROM schedules WHERE hive_id = ?",
                (item["id"],),
            ).fetchone()[0]
        else:
            item.setdefault("agent_count", 0)
            item.setdefault("issue_agent_count", 0)
            item.setdefault("subagent_enabled_count", 0)
            item.setdefault("open_task_count", 0)
            item.setdefault("schedule_count", 0)
        return item

    def list_agents(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [self.public_agent(row) for row in conn.execute("SELECT * FROM agents ORDER BY created_at DESC")]

    def create_agent(self, data: dict[str, Any]) -> dict[str, Any]:
        now = iso()
        provider = normalize_agent_provider_id(data.get("provider") or "local")
        max_subagents = int(data.get("max_subagents") or 0)
        issue_rate_limit_per_hour = int(data.get("issue_rate_limit_per_hour") or 0)
        issue_creation_enabled = bool(data.get("issue_creation_enabled", False))
        if max_subagents < 0:
            raise StoreError("max_subagents must be zero or greater")
        if issue_rate_limit_per_hour < 0:
            raise StoreError("issue_rate_limit_per_hour must be zero or greater")
        if issue_creation_enabled and issue_rate_limit_per_hour < 1:
            raise StoreError("issue creation agents require issue_rate_limit_per_hour >= 1")
        row = {
            "id": f"agent_{secrets.token_urlsafe(8)}",
            "name": data["name"],
            "role": data["role"],
            "provider": provider,
            "model": data.get("model") or self.config.agent_provider(provider).model,
            "status": "idle",
            "system_prompt": data.get("system_prompt") or "",
            "hive_id": data.get("hive_id") or None,
            "can_spawn_subagents": 1 if data.get("can_spawn_subagents", False) else 0,
            "max_subagents": max_subagents,
            "issue_creation_enabled": 1 if issue_creation_enabled else 0,
            "issue_kind": self.normalize_issue_kind(data.get("issue_kind")),
            "issue_rate_limit_per_hour": issue_rate_limit_per_hour,
            "issue_labels": dumps(self.normalize_labels(data.get("issue_labels"))),
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as conn:
            self.validate_optional_hive_reference(conn, field_name="hive_id", value=row["hive_id"])
            conn.execute(
                """
                INSERT INTO agents
                (id, name, role, provider, model, status, system_prompt, hive_id, can_spawn_subagents, max_subagents, issue_creation_enabled, issue_kind, issue_rate_limit_per_hour, issue_labels, created_at, updated_at)
                VALUES (:id, :name, :role, :provider, :model, :status, :system_prompt, :hive_id, :can_spawn_subagents, :max_subagents, :issue_creation_enabled, :issue_kind, :issue_rate_limit_per_hour, :issue_labels, :created_at, :updated_at)
                """,
                row,
            )
        self.audit(
            "agent.created",
            "operator",
            row["id"],
            "allowed",
            "agent registered",
            {
                "hive_id": row["hive_id"],
                "can_spawn_subagents": bool(row["can_spawn_subagents"]),
                "issue_creation_enabled": bool(row["issue_creation_enabled"]),
                "issue_kind": row["issue_kind"],
                "issue_rate_limit_per_hour": row["issue_rate_limit_per_hour"],
            },
        )
        return self.public_agent(row)

    def public_agent(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["can_spawn_subagents"] = bool(item["can_spawn_subagents"])
        item["issue_creation_enabled"] = bool(item["issue_creation_enabled"])
        item["max_subagents"] = int(item["max_subagents"])
        item["issue_rate_limit_per_hour"] = int(item["issue_rate_limit_per_hour"])
        item["issue_labels"] = loads(item["issue_labels"], [])
        return item

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
            if row is None:
                raise StoreNotFoundError(f"unknown agent: {agent_id}")
            return self.public_agent(row)

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
        if kind not in {"generic_reference", "github_oauth_app", "github_app", "managed_secret"}:
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

    def _prepare_credential_row(
        self,
        data: dict[str, Any],
        *,
        allow_managed_secret_metadata: bool = False,
    ) -> dict[str, Any]:
        now = iso()
        actions = sorted(set(action.strip().lower() for action in data["allowed_actions"] if action.strip()))
        agents = sorted(set(agent.strip() for agent in (data.get("allowed_agents") or []) if agent.strip()))
        approval_required_actions = sorted(
            set(action.strip().lower() for action in (data.get("approval_required_actions") or []) if action.strip())
        )
        provider = str(data["provider"]).strip().lower()
        name = str(data["name"]).strip()
        secret_ref = str(data.get("secret_ref") or "").strip()
        metadata = self.normalize_credential_metadata(provider, data.get("metadata"))
        if not allow_managed_secret_metadata:
            try:
                validate_external_credential_metadata(metadata)
            except ValueError as exc:
                raise StoreError(str(exc)) from exc
        if not actions:
            raise StoreError("credential must allow at least one action")
        if not set(approval_required_actions).issubset(actions):
            raise StoreError("approval_required_actions must be a subset of allowed_actions")
        if not name:
            raise StoreError("credential name is required")
        if not provider:
            raise StoreError("provider is required")
        if not secret_ref:
            raise StoreError("secret_ref is required")
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
        try:
            row["secret_ref"] = validate_external_secret_ref(row["secret_ref"])
        except ValueError as exc:
            raise StoreError(str(exc)) from exc
        with self.connect() as conn:
            conn.execute(CREDENTIAL_INSERT_SQL, row)
        return self.public_credential(row)

    def create_managed_credential(
        self,
        data: dict[str, Any],
        *,
        secret_value: str,
        secret_box: SecretBox,
    ) -> dict[str, Any]:
        if len(secret_value) == 0:
            raise StoreError("secret_value is required")
        credential_id = data.get("id") or f"cred_{secrets.token_urlsafe(8)}"
        metadata = dict(data.get("metadata") or {})
        metadata["credential_kind"] = "managed_secret"
        credential_row = self._prepare_credential_row(
            {
                **data,
                "id": credential_id,
                "secret_ref": f"{BROKER_SECRET_SCHEME}://{credential_id}",
                "metadata": metadata,
            },
            allow_managed_secret_metadata=True,
        )
        broker_secret_row = {
            "credential_id": credential_row["id"],
            "ciphertext": secret_box.encrypt_text(secret_value),
            "created_at": credential_row["created_at"],
            "updated_at": credential_row["updated_at"],
        }
        with self.connect() as conn:
            conn.execute(CREDENTIAL_INSERT_SQL, credential_row)
            conn.execute(
                """
                INSERT INTO broker_secrets
                (credential_id, ciphertext, created_at, updated_at)
                VALUES (:credential_id, :ciphertext, :created_at, :updated_at)
                """,
                broker_secret_row,
            )
        return self.public_credential(credential_row)

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
            conn.execute(CREDENTIAL_INSERT_SQL, credential_row)
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
            "metadata": redact_public_metadata_value(loads(row["metadata"], {})),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def resolve_broker_secret(self, credential_id: str, secret_box: SecretBox) -> str:
        credential = self.get_credential(credential_id)
        scheme, _, target = str(credential["secret_ref"]).partition("://")
        if scheme != BROKER_SECRET_SCHEME or target != credential_id:
            raise StoreError("credential does not use broker-managed secret storage")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT ciphertext FROM broker_secrets WHERE credential_id = ?",
                (credential_id,),
            ).fetchone()
        if row is None:
            raise StoreNotFoundError(f"missing broker secret for credential: {credential_id}")
        return secret_box.decrypt_text(row["ciphertext"])

    def request_lease(
        self,
        credential_id: str,
        agent_id: str,
        action: str,
        intent: str,
        ttl_seconds: int | None,
        *,
        audit_metadata: Mapping[str, Any] | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        self.get_agent(agent_id)
        credential = self.get_credential(credential_id)
        base_audit_metadata = dict(audit_metadata or {})
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
            self.audit(
                LEASE_DENIED_EVENT,
                agent_id,
                credential_id,
                "denied",
                review.reason,
                {"action": normalized_action, **base_audit_metadata},
            )
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
                {"action": normalized_action, "lease_id": row["id"], "ttl_seconds": ttl, **base_audit_metadata},
            )
            return None, self.public_lease(row)
        self.audit(
            "credential.lease.issued",
            agent_id,
            credential_id,
            "allowed",
            review.reason,
            {"action": normalized_action, "lease_id": row["id"], "ttl_seconds": ttl, **base_audit_metadata},
        )
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

    def validate_optional_hive_reference(
        self,
        conn: sqlite3.Connection,
        *,
        field_name: str,
        value: str | None,
    ) -> None:
        if value is None:
            return
        row = conn.execute("SELECT 1 FROM hives WHERE id = ?", (value,)).fetchone()
        if row is None:
            raise StoreValidationError(f"{field_name} references unknown hive: {value}")

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

    def resolve_hive_id_for_assignment(
        self,
        conn: sqlite3.Connection,
        *,
        requested_hive_id: str | None,
        assigned_agent_id: str | None,
    ) -> str | None:
        hive_id = requested_hive_id or None
        self.validate_optional_hive_reference(conn, field_name="hive_id", value=hive_id)
        if assigned_agent_id is None:
            return hive_id
        agent = conn.execute("SELECT hive_id FROM agents WHERE id = ?", (assigned_agent_id,)).fetchone()
        if agent is None:
            raise StoreValidationError(f"assigned_agent_id references unknown agent: {assigned_agent_id}")
        agent_hive_id = agent["hive_id"]
        if hive_id and agent_hive_id and agent_hive_id != hive_id:
            raise StoreValidationError("assigned agent belongs to a different hive")
        return hive_id or agent_hive_id

    def prepare_task_row(
        self,
        conn: sqlite3.Connection,
        data: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        task_time = now or utcnow()
        heartbeat_seconds = data.get("heartbeat_seconds")
        assigned_agent_id = data.get("assigned_agent_id") or None
        row = {
            "id": f"task_{secrets.token_urlsafe(10)}",
            "title": data["title"],
            "description": data.get("description") or "",
            "status": data.get("status") or "queued",
            "priority": data.get("priority") or "normal",
            "hive_id": self.resolve_hive_id_for_assignment(
                conn,
                requested_hive_id=data.get("hive_id") or None,
                assigned_agent_id=assigned_agent_id,
            ),
            "assigned_agent_id": assigned_agent_id,
            "credential_id": data.get("credential_id") or None,
            "action": data.get("action") or "",
            "intent": data.get("intent") or "",
            "heartbeat_seconds": heartbeat_seconds,
            "next_heartbeat_at": iso(task_time + timedelta(seconds=int(heartbeat_seconds))) if heartbeat_seconds else None,
            "created_at": iso(task_time),
            "updated_at": iso(task_time),
        }
        self.validate_optional_credential_reference(
            conn,
            field_name="credential_id",
            value=row["credential_id"],
        )
        return row

    def insert_task_row(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        try:
            conn.execute(
                """
                INSERT INTO tasks
                (id, title, description, status, priority, hive_id, assigned_agent_id, credential_id, action, intent, heartbeat_seconds, next_heartbeat_at, created_at, updated_at)
                VALUES (:id, :title, :description, :status, :priority, :hive_id, :assigned_agent_id, :credential_id, :action, :intent, :heartbeat_seconds, :next_heartbeat_at, :created_at, :updated_at)
                """,
                row,
            )
        except sqlite3.IntegrityError as exc:
            raise StoreValidationError("task references an unknown hive, agent, or credential") from exc

    def _insert_task(self, conn: sqlite3.Connection, data: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
        task_time = now or utcnow()
        row = self.prepare_task_row(conn, data, now=task_time)
        self.insert_task_row(conn, row)
        self._insert_audit(
            conn,
            "task.created",
            row["assigned_agent_id"] or "user",
            row["id"],
            "allowed",
            "task created",
            {"status": row["status"], "hive_id": row["hive_id"]},
            now=task_time,
        )
        return row

    def create_task(self, data: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as conn:
            return self._insert_task(conn, data)

    def insert_issue_request_denial(
        self,
        conn: sqlite3.Connection,
        *,
        agent_id: str,
        hive_id: str,
        reason: str,
        metadata: dict[str, Any],
    ) -> str:
        self._insert_audit(
            conn,
            ISSUE_REQUEST_DENIED_EVENT,
            agent_id,
            hive_id,
            "denied",
            reason,
            metadata,
        )
        return reason

    def get_agent_row(self, conn: sqlite3.Connection, agent_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            raise StoreValidationError(f"agent_id references unknown agent: {agent_id}")
        return row

    def count_issue_requests_in_window(
        self,
        conn: sqlite3.Connection,
        *,
        agent_id: str,
        hive_id: str,
        since: datetime,
    ) -> int:
        return conn.execute(
            """
            SELECT COUNT(*) FROM audit_events
            WHERE type = 'issue.request.created'
              AND actor_id = ?
              AND target_id = ?
              AND created_at >= ?
            """,
            (agent_id, hive_id, iso(since)),
        ).fetchone()[0]

    def issue_request_denial(
        self,
        conn: sqlite3.Connection,
        *,
        hive: sqlite3.Row,
        agent: sqlite3.Row,
        requested_kind: str | None,
        now: datetime,
    ) -> tuple[str | None, dict[str, Any], str | None, int, int]:
        if agent["hive_id"] != hive["id"]:
            return "issue agent is not assigned to this hive", {"agent_hive_id": agent["hive_id"]}, None, 0, 0
        if hive["status"] != "active":
            return "hive is paused", {}, None, 0, 0
        if not bool(agent["issue_creation_enabled"]):
            return "agent issue creation is disabled", {}, None, 0, 0
        issue_kind = self.normalize_issue_kind(requested_kind or agent["issue_kind"])
        if issue_kind != agent["issue_kind"]:
            reason = f"agent is configured for {agent['issue_kind']} requests"
            return reason, {"requested_kind": issue_kind, "agent_kind": agent["issue_kind"]}, issue_kind, 0, 0
        rate_limit = int(agent["issue_rate_limit_per_hour"])
        if rate_limit < 1:
            return "agent issue rate limit is not configured", {}, issue_kind, rate_limit, 0
        used = self.count_issue_requests_in_window(
            conn,
            agent_id=agent["id"],
            hive_id=hive["id"],
            since=now - timedelta(hours=1),
        )
        if used >= rate_limit:
            return "agent issue request rate limit exceeded", {"limit_per_hour": rate_limit, "used_last_hour": used}, issue_kind, rate_limit, used
        return None, {}, issue_kind, rate_limit, used

    def queue_issue_request_task(
        self,
        conn: sqlite3.Connection,
        *,
        hive: sqlite3.Row,
        agent: sqlite3.Row,
        title: str,
        description: str,
        priority: str,
        labels: list[str],
        issue_kind: str,
        rate_limit: int,
        used: int,
    ) -> dict[str, Any]:
        merged_labels = sorted(set(loads(agent["issue_labels"], [])) | set(labels))
        tracker_project = hive["tracker_project"] or hive["project_ref"]
        task = self.prepare_task_row(
            conn,
            {
                "title": title,
                "description": description,
                "priority": priority,
                "hive_id": hive["id"],
                "assigned_agent_id": agent["id"],
                "credential_id": hive["tracker_credential_id"],
                "action": ISSUE_ACTION_BY_KIND[issue_kind],
                "intent": (
                    f"Queue a {issue_kind} request for {hive['tracker_provider']} tracker "
                    f"{tracker_project}. Follow hive guidance and use brokered credentials only."
                ),
                "heartbeat_seconds": None,
            },
        )
        self.insert_task_row(conn, task)
        self._insert_audit(
            conn,
            "task.created",
            agent["id"],
            task["id"],
            "allowed",
            "task created",
            {"status": task["status"], "hive_id": hive["id"], "source": "issue_request"},
        )
        self._insert_audit(
            conn,
            "issue.request.created",
            agent["id"],
            hive["id"],
            "allowed",
            "issue request queued",
            {
                "task_id": task["id"],
                "kind": issue_kind,
                "labels": merged_labels,
                "tracker_provider": hive["tracker_provider"],
                "tracker_project": tracker_project,
                "limit_per_hour": rate_limit,
                "remaining_this_hour": max(rate_limit - used - 1, 0),
                "can_spawn_subagents": bool(agent["can_spawn_subagents"]),
                "max_subagents": int(agent["max_subagents"]),
            },
        )
        return task

    def create_issue_request(self, data: dict[str, Any]) -> dict[str, Any]:
        now = utcnow()
        hive_id = data["hive_id"]
        agent_id = data["agent_id"]
        title = str(data["title"]).strip()
        description = str(data.get("description") or "").strip()
        priority = str(data.get("priority") or "normal").strip() or "normal"
        if not title:
            raise StoreError("issue request title is required")
        labels = self.normalize_labels(data.get("labels"))
        task: dict[str, Any] | None = None
        error_detail: str | None = None
        with self._lock:
            with self.connect() as conn:
                hive = self.get_hive_row(conn, hive_id)
                agent = self.get_agent_row(conn, agent_id)
                reason, metadata, issue_kind, rate_limit, used = self.issue_request_denial(
                    conn,
                    hive=hive,
                    agent=agent,
                    requested_kind=data.get("kind"),
                    now=now,
                )
                if reason is not None:
                    error_detail = self.insert_issue_request_denial(
                        conn,
                        agent_id=agent_id,
                        hive_id=hive_id,
                        reason=reason,
                        metadata=metadata,
                    )
                elif issue_kind is not None:
                    task = self.queue_issue_request_task(
                        conn,
                        hive=hive,
                        agent=agent,
                        title=title,
                        description=description,
                        priority=priority,
                        labels=labels,
                        issue_kind=issue_kind,
                        rate_limit=rate_limit,
                        used=used,
                    )
        if error_detail is not None:
            raise StoreError(error_detail)
        if task is None:
            raise RuntimeError("issue request flow ended without a task or denial")
        return task

    def list_tasks(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY created_at DESC")]

    def update_task_status(self, task_id: str, status: str) -> dict[str, Any]:
        now = iso()
        with self.connect() as conn:
            row = self.get_task_row(conn, task_id)
            conn.execute(TASK_STATUS_UPDATE_SQL, (status, now, task_id))
        self.audit("task.status.updated", row["assigned_agent_id"] or "user", task_id, "allowed", f"task marked {status}", {})
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            return dict(self.get_task_row(conn, task_id))

    def run_task(self, task_id: str, operator_input: str | None = None) -> dict[str, Any]:
        with self.connect() as conn:
            task = dict(self.get_task_row(conn, task_id))
            if not task["assigned_agent_id"]:
                raise StoreValidationError("task must be assigned to an agent before execution")
            if task["status"] != "queued":
                raise StoreError("only queued tasks can be executed")
            agent = conn.execute("SELECT * FROM agents WHERE id = ?", (task["assigned_agent_id"],)).fetchone()
            if agent is None:
                raise StoreValidationError(f"assigned_agent_id references unknown agent: {task['assigned_agent_id']}")
            agent = dict(agent)
            provider_id = normalize_agent_provider_id(agent["provider"])
            provider_config = self.config.agent_provider(provider_id)
            model = agent["model"] or provider_config.model
            now = iso()
            self.claim_queued_task_for_execution(conn, task_id, now)
            conn.execute("UPDATE agents SET status = ?, updated_at = ? WHERE id = ?", ("working", now, agent["id"]))
            self._insert_audit(
                conn,
                "task.execution.started",
                agent["id"],
                task_id,
                "allowed",
                "task execution started",
                {"provider": provider_id, "model": model},
            )

        prompt = (operator_input or "").strip() or task["description"].strip() or task["title"].strip()
        if not prompt:
            self._finish_task_execution(
                task_id=task_id,
                agent_id=agent["id"],
                status="failed",
                decision="denied",
                reason="task execution prompt is required",
                metadata={"provider": provider_id, "model": model},
            )
            raise StoreValidationError("task execution prompt is required")

        if provider_id not in CREDENTIAL_OPTIONAL_AGENT_PROVIDERS and not provider_config.credential_id:
            reason = f"agent provider credential_id is not configured: {provider_id}"
            self._finish_task_execution(
                task_id=task_id,
                agent_id=agent["id"],
                status="failed",
                decision="denied",
                reason=reason,
                metadata={"provider": provider_id, "model": model},
            )
            raise StoreError(reason)

        if not self._agent_provider_registry.has_adapter(provider_id):
            reason = f"agent provider adapter is not configured: {provider_id}"
            self._finish_task_execution(
                task_id=task_id,
                agent_id=agent["id"],
                status="failed",
                decision="denied",
                reason=reason,
                metadata={"provider": provider_id, "model": model},
            )
            raise StoreError(reason)

        tool_request = self.authorize_task_provider_tool_request(
            task=task,
            agent_id=agent["id"],
            provider_id=provider_id,
            model=model,
        )
        tool_requests = (tool_request,) if tool_request is not None else ()
        provider_credential_action = None
        if provider_id not in CREDENTIAL_OPTIONAL_AGENT_PROVIDERS:
            provider_credential_action = self.authorize_agent_provider_credential(
                task_id=task_id,
                agent_id=agent["id"],
                provider_id=provider_id,
                model=model,
                credential_id=provider_config.credential_id or "",
            )
        request = ProviderRunRequest(
            provider=provider_id,
            model=model,
            prompt=prompt,
            system_prompt=agent["system_prompt"],
            messages=(ProviderMessage(role="user", content=prompt),),
            tool_requests=tuple(tool_requests),
            credential_id=provider_config.credential_id,
            credential_action=provider_credential_action,
            metadata={"task_id": task_id},
        )
        try:
            result = self._agent_provider_registry.run(request)
        except AgentProviderError as exc:
            self._finish_task_execution(
                task_id=task_id,
                agent_id=agent["id"],
                status="failed",
                decision="denied",
                reason=AGENT_PROVIDER_FAILED_CLOSED_REASON,
                metadata={"provider": provider_id, "model": request.model},
            )
            raise StoreError(AGENT_PROVIDER_FAILED_CLOSED_REASON) from exc
        except Exception as exc:
            self._finish_task_execution(
                task_id=task_id,
                agent_id=agent["id"],
                status="failed",
                decision="denied",
                reason=AGENT_PROVIDER_FAILED_CLOSED_REASON,
                metadata={"provider": provider_id, "model": request.model},
            )
            raise StoreError(AGENT_PROVIDER_FAILED_CLOSED_REASON) from exc

        self._finish_task_execution(
            task_id=task_id,
            agent_id=agent["id"],
            status="done",
            decision="allowed",
            reason="task executed through agent provider adapter",
            metadata={"provider": provider_id, "model": request.model},
        )
        return {
            "task_id": task_id,
            "agent_id": agent["id"],
            **redact_provider_public_value(result.public_view(), None),
        }

    def authorize_agent_provider_credential(
        self,
        *,
        task_id: str,
        agent_id: str,
        provider_id: str,
        model: str,
        credential_id: str,
    ) -> dict[str, Any]:
        action = f"{AGENT_PROVIDER_CREDENTIAL_ACTION_PREFIX}{provider_id}"
        intent = f"Run task {task_id} through the {provider_id} agent provider using model {model}."
        try:
            token, lease = self.request_lease(
                credential_id=credential_id,
                agent_id=agent_id,
                action=action,
                intent=intent,
                ttl_seconds=60,
                audit_metadata={
                    "task_id": task_id,
                    "provider": provider_id,
                    "model": model,
                    "capability": "agent_provider",
                },
            )
            if token is None:
                reason = "agent provider credential requires operator-approved lease"
                self._finish_task_execution(
                    task_id=task_id,
                    agent_id=agent_id,
                    status="failed",
                    decision="denied",
                    reason=reason,
                    metadata={
                        "provider": provider_id,
                        "model": model,
                        "credential_id": credential_id,
                        "action": lease["action"],
                        "lease_id": lease["id"],
                    },
                )
                raise StoreError(reason)
            return self.perform_credential_action(
                lease_token=token,
                action=action,
                payload={
                    "task_id": task_id,
                    "provider": provider_id,
                    "model": model,
                    "capability": "agent_provider",
                },
            )
        except StoreError as exc:
            if str(exc) != "agent provider credential requires operator-approved lease":
                self._finish_task_execution(
                    task_id=task_id,
                    agent_id=agent_id,
                    status="failed",
                    decision="denied",
                    reason=str(exc),
                    metadata={
                        "provider": provider_id,
                        "model": model,
                        "credential_id": credential_id,
                        "action": action,
                    },
                )
            raise

    def authorize_task_provider_tool_request(
        self,
        *,
        task: Mapping[str, Any],
        agent_id: str,
        provider_id: str,
        model: str,
    ) -> ProviderToolRequest | None:
        if not task["credential_id"] or not task["action"]:
            return None
        action = str(task["action"]).strip().lower()
        try:
            token, lease = self.request_lease(
                credential_id=task["credential_id"],
                agent_id=agent_id,
                action=task["action"],
                intent=task["intent"],
                ttl_seconds=None,
                audit_metadata={
                    "task_id": task["id"],
                    "provider": provider_id,
                    "model": model,
                    "capability": "provider_tool",
                },
            )
            if token is None:
                denied_reason = "credential action requires operator-approved lease"
                self._finish_task_execution(
                    task_id=task["id"],
                    agent_id=agent_id,
                    status="failed",
                    decision="denied",
                    reason=denied_reason,
                    metadata={
                        "provider": provider_id,
                        "model": model,
                        "credential_id": task["credential_id"],
                        "action": lease["action"],
                        "lease_id": lease["id"],
                    },
                )
                raise StoreError(denied_reason)
            credential_action = self.perform_credential_action(
                lease_token=token,
                action=task["action"],
                payload={
                    "task_id": task["id"],
                    "provider": provider_id,
                    "model": model,
                    "capability": "provider_tool",
                },
            )
        except StoreError as exc:
            if str(exc) != "credential action requires operator-approved lease":
                self._finish_task_execution(
                    task_id=task["id"],
                    agent_id=agent_id,
                    status="failed",
                    decision="denied",
                    reason=str(exc),
                    metadata={
                        "provider": provider_id,
                        "model": model,
                        "credential_id": task["credential_id"],
                        "action": action,
                    },
                )
            raise

        return ProviderToolRequest(
            name=credential_action["action"],
            arguments={
                "credential_id": task["credential_id"],
                "action": credential_action["action"],
                "intent": task["intent"],
                "credential_action": credential_action,
            },
        )

    def claim_queued_task_for_execution(self, conn: sqlite3.Connection, task_id: str, now: str) -> None:
        claim = conn.execute(TASK_RUN_CLAIM_SQL, ("running", now, task_id, "queued"))
        if claim.rowcount != 1:
            raise StoreError("only queued tasks can be executed")

    def _finish_task_execution(
        self,
        *,
        task_id: str,
        agent_id: str,
        status: str,
        decision: str,
        reason: str,
        metadata: dict[str, Any],
    ) -> None:
        now = iso()
        with self.connect() as conn:
            conn.execute(TASK_STATUS_UPDATE_SQL, (status, now, task_id))
            running_task = conn.execute(
                """
                SELECT 1
                FROM tasks
                WHERE assigned_agent_id = ?
                  AND status = ?
                  AND id != ?
                LIMIT 1
                """,
                (agent_id, "running", task_id),
            ).fetchone()
            if running_task is None:
                conn.execute(
                    "UPDATE agents SET status = ?, updated_at = ? WHERE id = ?",
                    ("idle", now, agent_id),
                )
        event_type = "task.execution.completed" if status == "done" else "task.execution.failed"
        self.audit(event_type, agent_id, task_id, decision, reason, metadata)

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
            "hive_id": None,
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
            row["hive_id"] = self.resolve_hive_id_for_assignment(
                conn,
                requested_hive_id=data.get("hive_id") or None,
                assigned_agent_id=row["assigned_agent_id"],
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
                    (id, name, enabled, interval_seconds, catch_up_policy, task_title, task_description, priority, hive_id, assigned_agent_id, credential_id, action, intent, next_run_at, last_run_at, created_at, updated_at)
                    VALUES (:id, :name, :enabled, :interval_seconds, :catch_up_policy, :task_title, :task_description, :priority, :hive_id, :assigned_agent_id, :credential_id, :action, :intent, :next_run_at, :last_run_at, :created_at, :updated_at)
                    """,
                    row,
                )
            except sqlite3.IntegrityError as exc:
                raise StoreValidationError("schedule references an unknown hive, agent, or credential") from exc
        self.audit(
            "schedule.created",
            row["assigned_agent_id"] or "user",
            row["id"],
            "allowed",
            "schedule created",
            {"interval_seconds": interval, "catch_up_policy": catch_up_policy, "hive_id": row["hive_id"]},
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
                conn.execute(BEGIN_IMMEDIATE_SQL)
                enabled_rows = list(conn.execute("SELECT * FROM schedules WHERE enabled = 1"))
                due_rows: list[tuple[datetime, sqlite3.Row]] = []
                for row in enabled_rows:
                    next_run_at = require_aware_utc(row["next_run_at"], field_name="next_run_at")
                    if next_run_at <= now:
                        due_rows.append((next_run_at, row))
                due_rows.sort(key=lambda item: item[0])
                for next_run_at, row in due_rows:
                    interval_seconds = int(row["interval_seconds"])
                    interval = timedelta(seconds=interval_seconds)
                    catch_up_policy = row["catch_up_policy"] or "run_once"
                    if catch_up_policy not in SCHEDULE_CATCH_UP_POLICIES:
                        raise StoreError(f"unsupported catch-up policy: {catch_up_policy}")
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
                        task = self._insert_task(
                            conn,
                            {
                                "title": row["task_title"],
                                "description": row["task_description"],
                                "priority": row["priority"],
                                "hive_id": row["hive_id"],
                                "assigned_agent_id": row["assigned_agent_id"],
                                "credential_id": row["credential_id"],
                                "action": row["action"],
                                "intent": row["intent"],
                                "heartbeat_seconds": None,
                            },
                            now=now,
                        )
                        created.append(task)
                        task_ids.append(task["id"])
                    conn.execute(
                        "UPDATE schedules SET last_run_at = ?, next_run_at = ?, updated_at = ? WHERE id = ?",
                        (iso(now), iso(next_run), iso(now), row["id"]),
                    )
                    self._insert_audit(
                        conn,
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
                            "hive_id": row["hive_id"],
                            "task_ids": task_ids,
                        },
                        now=now,
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
        *,
        now: datetime | None = None,
    ) -> None:
        audit_time = now or utcnow()
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
                "created_at": iso(audit_time),
            },
        )
