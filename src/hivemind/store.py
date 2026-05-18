from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
import hashlib
import hmac
import json
import logging
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
from hivemind.models import (
    DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
    INITIAL_TASK_STATUSES,
    TASK_STATUS_TRANSITIONS,
    TERMINAL_TASK_STATUSES,
    TaskPriority,
    TaskStatus,
)
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
from hivemind.tool_registry import (
    DEFAULT_TOOL_ACTIONS,
    TOOL_ACTION_RISK_LEVELS,
    normalize_tool_action_name,
    payload_schema_error,
    validate_tool_action_schema,
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
LEASE_DENIED_EVENT = "credential.lease.denied"
LEASE_REQUEST_COUNTED_METADATA_KEY = "lease_request_counted"
LEASE_REQUEST_RATE_LIMIT_EVENTS = (
    "credential.lease.issued",
    "credential.lease.pending",
    LEASE_DENIED_EVENT,
)
ACTION_RATE_LIMIT_EVENTS = ("credential.action.performed",)
CREDENTIAL_BY_ID_QUERY = "SELECT * FROM credentials WHERE id = ?"


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


ACTION_DENIED_EVENT = "credential.action.denied"
ISSUE_REQUEST_DENIED_EVENT = "issue.request.denied"
AGENT_PROVIDER_FAILED_CLOSED_REASON = "agent provider failed closed"
AGENT_PROVIDER_CREDENTIAL_ACTION_PREFIX = "agent_provider_"
LEGACY_AGENT_PROVIDER_CREDENTIAL_ACTION_PREFIX = "agent_provider:"
REDACTED_VALUE = "[redacted]"
SENSITIVE_LOG_KEY_FRAGMENTS = (
    "authorization",
    "ciphertext",
    "code_verifier",
    "password",
    "secret",
    "token",
)
AUDIT_LOGGER = logging.getLogger("hivemind.audit")
STRUCTURED_AUDIT_LOG_PREFIXES = ("credential.lease.", "credential.action.")
TASK_BY_ID_QUERY = "SELECT * FROM tasks WHERE id = ?"
AGENT_STATUS_ALIASES = {"working": "running"}
AGENT_STATUS_VALUES = frozenset({"idle", "queued", "running", "blocked", "done", "failed"})
FINAL_TASK_STATUSES = frozenset({"done", "failed", "cancelled"})
AGENT_STATUS_UPDATE_SQL = "UPDATE agents SET status = ?, updated_at = ? WHERE id = ?"
TASK_STATUS_UPDATE_SQL = "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?"
VALID_TASK_PRIORITIES = frozenset(priority.value for priority in TaskPriority)
VALID_TASK_STATUSES = frozenset(status.value for status in TaskStatus)
VALID_INITIAL_TASK_STATUSES = frozenset(status.value for status in INITIAL_TASK_STATUSES)
TERMINAL_TASK_STATUS_VALUES = frozenset(status.value for status in TERMINAL_TASK_STATUSES)
EDITABLE_TASK_FIELDS = (
    "title",
    "description",
    "priority",
    "assigned_agent_id",
    "credential_id",
    "action",
    "intent",
    "heartbeat_seconds",
)
VALID_TASK_STATUS_TRANSITIONS = {
    status.value: frozenset(next_status.value for next_status in next_statuses)
    for status, next_statuses in TASK_STATUS_TRANSITIONS.items()
}
TASK_RUN_CLAIM_SQL = "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND status = ?"
SCHEDULE_BY_ID_QUERY = "SELECT * FROM schedules WHERE id = ?"
BEGIN_IMMEDIATE_SQL = "BEGIN IMMEDIATE"
AGENT_BY_ID_QUERY = "SELECT * FROM agents WHERE id = ?"
BROKER_SECRET_SCHEME = ALLOWED_SECRET_REF_SCHEMES[-1]
CREDENTIAL_INSERT_SQL = """
    INSERT INTO credentials
    (
        id, name, provider, secret_ref, allowed_agents, allowed_actions,
        approval_required_actions, max_ttl_seconds, require_intent,
        agent_lease_limit, credential_lease_limit, credential_action_limit,
        rate_limit_window_seconds, provider_token_budget, provider_cost_budget_cents,
        metadata, created_at, updated_at
    )
    VALUES (
        :id, :name, :provider, :secret_ref, :allowed_agents, :allowed_actions,
        :approval_required_actions, :max_ttl_seconds, :require_intent,
        :agent_lease_limit, :credential_lease_limit, :credential_action_limit,
        :rate_limit_window_seconds, :provider_token_budget, :provider_cost_budget_cents,
        :metadata, :created_at, :updated_at
    )
"""
SAFE_ACTION_NAME = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
BACKUP_FORMAT = "hivemind-logical-backup"
BACKUP_LEGACY_FORMAT_VERSION = 1
BACKUP_FORMAT_VERSION = 2
BACKUP_SUPPORTED_FORMAT_VERSIONS = (BACKUP_LEGACY_FORMAT_VERSION, BACKUP_FORMAT_VERSION)
BACKUP_TABLE_QUERIES: dict[str, str] = {
    "users": "SELECT id, username, password_hash, role, created_at FROM users ORDER BY id",
    "credentials": (
        "SELECT * FROM credentials "
        "WHERE secret_ref NOT LIKE 'oauth://%' AND secret_ref NOT LIKE 'secret://%' "
        "ORDER BY id"
    ),
    "hives": (
        "SELECT id, name, project_ref, tracker_provider, tracker_project, tracker_base_url, "
        "tracker_credential_id, guidance, status, created_at, updated_at FROM hives ORDER BY id"
    ),
    "agents": (
        "SELECT id, name, role, provider, model, status, system_prompt, hive_id, "
        "can_spawn_subagents, max_subagents, issue_creation_enabled, issue_kind, "
        "issue_rate_limit_per_hour, issue_labels, created_at, updated_at FROM agents ORDER BY id"
    ),
    "tool_actions": "SELECT * FROM tool_actions ORDER BY name",
    "tasks": (
        "SELECT id, title, description, status, priority, hive_id, assigned_agent_id, credential_id, "
        "action, intent, heartbeat_seconds, next_heartbeat_at, created_at, updated_at FROM tasks ORDER BY id"
    ),
    "schedules": (
        "SELECT id, name, enabled, interval_seconds, catch_up_policy, task_title, task_description, "
        "priority, hive_id, assigned_agent_id, credential_id, action, intent, next_run_at, last_run_at, "
        "created_at, updated_at FROM schedules ORDER BY id"
    ),
    "heartbeat_events": "SELECT * FROM heartbeat_events ORDER BY id",
    "audit_events": "SELECT * FROM audit_events ORDER BY id",
}
BACKUP_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "users": ("id", "username", "password_hash", "role", "created_at"),
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
        "agent_lease_limit",
        "credential_lease_limit",
        "credential_action_limit",
        "rate_limit_window_seconds",
        "provider_token_budget",
        "provider_cost_budget_cents",
        "metadata",
        "created_at",
        "updated_at",
    ),
    "hives": (
        "id",
        "name",
        "project_ref",
        "tracker_provider",
        "tracker_project",
        "tracker_base_url",
        "tracker_credential_id",
        "guidance",
        "status",
        "created_at",
        "updated_at",
    ),
    "agents": (
        "id",
        "name",
        "role",
        "provider",
        "model",
        "status",
        "system_prompt",
        "hive_id",
        "can_spawn_subagents",
        "max_subagents",
        "issue_creation_enabled",
        "issue_kind",
        "issue_rate_limit_per_hour",
        "issue_labels",
        "created_at",
        "updated_at",
    ),
    "tasks": (
        "id",
        "title",
        "description",
        "status",
        "priority",
        "hive_id",
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
        "hive_id",
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
    "tool_actions": (
        "name",
        "description",
        "input_schema",
        "required_credential_action",
        "risk_level",
        "created_at",
        "updated_at",
    ),
}
BACKUP_LEGACY_V1_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "users": BACKUP_TABLE_COLUMNS["users"],
    "credentials": BACKUP_TABLE_COLUMNS["credentials"],
    "agents": ("id", "name", "role", "provider", "model", "status", "system_prompt", "created_at", "updated_at"),
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
    "heartbeat_events": BACKUP_TABLE_COLUMNS["heartbeat_events"],
    "audit_events": BACKUP_TABLE_COLUMNS["audit_events"],
}
BACKUP_CREDENTIAL_ROW_DEFAULTS: dict[str, Any] = {
    "agent_lease_limit": None,
    "credential_lease_limit": None,
    "credential_action_limit": None,
    "rate_limit_window_seconds": DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
    "provider_token_budget": None,
    "provider_cost_budget_cents": None,
}
BACKUP_INSERT_STATEMENTS: dict[str, str] = {
    "users": (
        "INSERT INTO users (id, username, password_hash, role, created_at) "
        "VALUES (:id, :username, :password_hash, :role, :created_at)"
    ),
    "credentials": (
        "INSERT INTO credentials (id, name, provider, secret_ref, allowed_agents, allowed_actions, "
        "approval_required_actions, max_ttl_seconds, require_intent, "
        "agent_lease_limit, credential_lease_limit, credential_action_limit, "
        "rate_limit_window_seconds, provider_token_budget, provider_cost_budget_cents, "
        "metadata, created_at, updated_at) "
        "VALUES (:id, :name, :provider, :secret_ref, :allowed_agents, :allowed_actions, "
        ":approval_required_actions, :max_ttl_seconds, :require_intent, "
        ":agent_lease_limit, :credential_lease_limit, :credential_action_limit, "
        ":rate_limit_window_seconds, :provider_token_budget, :provider_cost_budget_cents, "
        ":metadata, :created_at, :updated_at)"
    ),
    "hives": (
        "INSERT INTO hives (id, name, project_ref, tracker_provider, tracker_project, tracker_base_url, "
        "tracker_credential_id, guidance, status, created_at, updated_at) "
        "VALUES (:id, :name, :project_ref, :tracker_provider, :tracker_project, :tracker_base_url, "
        ":tracker_credential_id, :guidance, :status, :created_at, :updated_at)"
    ),
    "agents": (
        "INSERT INTO agents (id, name, role, provider, model, status, system_prompt, hive_id, "
        "can_spawn_subagents, max_subagents, issue_creation_enabled, issue_kind, issue_rate_limit_per_hour, "
        "issue_labels, created_at, updated_at) "
        "VALUES (:id, :name, :role, :provider, :model, :status, :system_prompt, :hive_id, "
        ":can_spawn_subagents, :max_subagents, :issue_creation_enabled, :issue_kind, "
        ":issue_rate_limit_per_hour, :issue_labels, :created_at, :updated_at)"
    ),
    "tool_actions": (
        "INSERT INTO tool_actions (name, description, input_schema, required_credential_action, risk_level, created_at, updated_at) "
        "VALUES (:name, :description, :input_schema, :required_credential_action, :risk_level, :created_at, :updated_at)"
    ),
    "tasks": (
        "INSERT INTO tasks (id, title, description, status, priority, hive_id, assigned_agent_id, credential_id, "
        "action, intent, heartbeat_seconds, next_heartbeat_at, created_at, updated_at) "
        "VALUES (:id, :title, :description, :status, :priority, :hive_id, :assigned_agent_id, :credential_id, "
        ":action, :intent, :heartbeat_seconds, :next_heartbeat_at, :created_at, :updated_at)"
    ),
    "schedules": (
        "INSERT INTO schedules (id, name, enabled, interval_seconds, catch_up_policy, task_title, task_description, priority, "
        "hive_id, assigned_agent_id, credential_id, action, intent, next_run_at, last_run_at, created_at, updated_at) "
        "VALUES (:id, :name, :enabled, :interval_seconds, :catch_up_policy, :task_title, :task_description, :priority, "
        ":hive_id, :assigned_agent_id, :credential_id, :action, :intent, :next_run_at, :last_run_at, :created_at, :updated_at)"
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
    "DELETE FROM agents",
    "DELETE FROM hives",
    "DELETE FROM credentials",
    "DELETE FROM tool_actions",
    "DELETE FROM users",
)
BACKUP_CREDENTIAL_REFERENCE_FIELDS = {
    "hives": "tracker_credential_id",
    "tasks": "credential_id",
    "schedules": "credential_id",
}
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


def is_sensitive_log_key(key: str) -> bool:
    normalized = key.lower()
    return any(fragment in normalized for fragment in SENSITIVE_LOG_KEY_FRAGMENTS)


def sanitize_log_value(key: str | None, value: Any) -> Any:
    if key and is_sensitive_log_key(key):
        return REDACTED_VALUE
    if isinstance(value, Mapping):
        return {str(item_key): sanitize_log_value(str(item_key), item_value) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [sanitize_log_value(key, item) for item in value]
    return value


def should_emit_structured_audit_log(event_type: str) -> bool:
    return event_type.startswith(STRUCTURED_AUDIT_LOG_PREFIXES)


@dataclass(frozen=True)
class SessionUser:
    id: str
    username: str
    role: str


@dataclass(frozen=True)
class RateLimitDenial:
    reason: str
    metadata: dict[str, Any]


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
                  agent_lease_limit INTEGER,
                  credential_lease_limit INTEGER,
                  credential_action_limit INTEGER,
                  rate_limit_window_seconds INTEGER NOT NULL DEFAULT 60,
                  provider_token_budget INTEGER,
                  provider_cost_budget_cents INTEGER,
                  metadata TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tool_actions (
                  name TEXT PRIMARY KEY,
                  description TEXT NOT NULL,
                  input_schema TEXT NOT NULL,
                  required_credential_action TEXT NOT NULL,
                  risk_level TEXT NOT NULL,
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_assigned_agent_id ON tasks(assigned_agent_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_schedules_assigned_agent_id ON schedules(assigned_agent_id)")
            self._migrate_sessions_to_token_hashes(conn)
            self._migrate_users_to_username(conn)
            self._migrate_legacy_agent_statuses(conn)
            self._migrate_schedules_to_catch_up_policy(conn)
            self._migrate_credentials_to_approval_actions(conn)
            self._migrate_credentials_to_rate_limits(conn)
            self._migrate_leases_to_store_ttl(conn)
            self._migrate_terminal_task_heartbeats(conn)
            self._seed_default_tool_actions(conn)

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

    def _migrate_legacy_agent_statuses(self, conn: sqlite3.Connection) -> None:
        if conn.execute("SELECT 1 FROM agents WHERE status = 'working' LIMIT 1").fetchone() is None:
            return
        conn.execute(
            "UPDATE agents SET status = 'running', updated_at = ? WHERE status = 'working'",
            (iso(),),
        )

    def _migrate_credentials_to_approval_actions(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(credentials)")}
        if "approval_required_actions" in columns:
            return
        conn.execute("ALTER TABLE credentials ADD COLUMN approval_required_actions TEXT NOT NULL DEFAULT '[]'")

    def _migrate_credentials_to_rate_limits(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(credentials)")}
        migrations = {
            "agent_lease_limit": "ALTER TABLE credentials ADD COLUMN agent_lease_limit INTEGER",
            "credential_lease_limit": "ALTER TABLE credentials ADD COLUMN credential_lease_limit INTEGER",
            "credential_action_limit": "ALTER TABLE credentials ADD COLUMN credential_action_limit INTEGER",
            "rate_limit_window_seconds": (
                "ALTER TABLE credentials ADD COLUMN rate_limit_window_seconds INTEGER NOT NULL DEFAULT 60"
            ),
            "provider_token_budget": "ALTER TABLE credentials ADD COLUMN provider_token_budget INTEGER",
            "provider_cost_budget_cents": "ALTER TABLE credentials ADD COLUMN provider_cost_budget_cents INTEGER",
        }
        for column, statement in migrations.items():
            if column not in columns:
                conn.execute(statement)

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

    def _migrate_terminal_task_heartbeats(self, conn: sqlite3.Connection) -> None:
        terminal_statuses = tuple(sorted(TERMINAL_TASK_STATUS_VALUES))
        conn.execute(
            """
            UPDATE tasks
            SET next_heartbeat_at = NULL
            WHERE status IN (?, ?, ?)
              AND next_heartbeat_at IS NOT NULL
            """,
            terminal_statuses,
        )

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
        for table, field in BACKUP_CREDENTIAL_REFERENCE_FIELDS.items():
            normalized[table] = [
                {
                    **row,
                    field: row[field] if row.get(field) in credential_ids else None,
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
            for field_name in (
                "agent_lease_limit",
                "credential_lease_limit",
                "credential_action_limit",
                "provider_token_budget",
                "provider_cost_budget_cents",
            ):
                row[field_name] = self.normalize_optional_positive_int(row.get(field_name), field_name=field_name)
            row["rate_limit_window_seconds"] = self.normalize_positive_int(
                row.get("rate_limit_window_seconds"),
                field_name="rate_limit_window_seconds",
                default=DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
            )
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

    def validate_backup_tool_action_row(self, row: dict[str, Any]) -> dict[str, Any]:
        try:
            schema = loads(str(row["input_schema"]), {})
            normalized_schema = validate_tool_action_schema(schema)
            name = self.normalize_action_name(str(row["name"]))
            required_action = self.normalize_action_name(str(row["required_credential_action"]))
        except (TypeError, ValueError) as exc:
            raise StoreValidationError(str(exc)) from exc
        risk_level = str(row["risk_level"]).strip().lower()
        if risk_level not in TOOL_ACTION_RISK_LEVELS:
            raise StoreValidationError("tool action risk_level must be low, medium, or high")
        row["name"] = name
        row["description"] = str(row["description"] or "").strip()
        row["input_schema"] = dumps(normalized_schema)
        row["required_credential_action"] = required_action
        row["risk_level"] = risk_level
        row["created_at"] = str(row["created_at"])
        row["updated_at"] = str(row["updated_at"])
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
        row_validator = {
            "credentials": self.validate_backup_credential_row,
            "tool_actions": self.validate_backup_tool_action_row,
            "schedules": self.validate_backup_schedule_row,
        }.get(table)
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
            if table == "credentials":
                row_dict = {**BACKUP_CREDENTIAL_ROW_DEFAULTS, **row_dict}
            missing_columns = [column for column in columns if column not in row_dict]
            if missing_columns:
                missing = ", ".join(missing_columns)
                raise StoreValidationError(f"backup table {table} row {index} is missing columns: {missing}")
            if row_validator is not None:
                row_dict = row_validator(row_dict)
            normalized_rows.append(row_dict)
        return normalized_rows

    def legacy_backup_tool_action_rows(self, legacy_tables: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        default_actions = {
            action["name"]: action
            for action in (*DEFAULT_TOOL_ACTIONS, *self._default_agent_provider_tool_actions())
        }
        action_names = set(default_actions)
        for credential in legacy_tables["credentials"]:
            action_names.update(self._json_action_names(str(credential.get("allowed_actions"))))
            action_names.update(self._json_action_names(str(credential.get("approval_required_actions"))))
        for table in ("tasks", "schedules"):
            for row in legacy_tables[table]:
                action = str(row.get("action") or "")
                if not action.strip():
                    continue
                try:
                    action_names.add(self.normalize_action_name(action))
                except StoreError:
                    continue

        now = iso()
        rows: list[dict[str, Any]] = []
        for action_name in sorted(action_names):
            action = default_actions.get(action_name)
            if action is None:
                action = {
                    "name": action_name,
                    "description": "Restored legacy action.",
                    "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": True},
                    "required_credential_action": action_name,
                    "risk_level": "medium",
                }
            rows.append(
                {
                    "name": action["name"],
                    "description": action["description"],
                    "input_schema": dumps(action["input_schema"]),
                    "required_credential_action": action["required_credential_action"],
                    "risk_level": action["risk_level"],
                    "created_at": now,
                    "updated_at": now,
                }
            )
        return rows

    def normalize_legacy_v1_backup_tables(
        self,
        tables: Mapping[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        missing_tables = [table for table in BACKUP_LEGACY_V1_TABLE_COLUMNS if table not in tables]
        if missing_tables:
            missing = ", ".join(missing_tables)
            raise StoreValidationError(f"backup bundle is missing required tables: {missing}")

        legacy_tables = {
            table: self.validate_backup_rows(
                table=table,
                rows=tables[table],
                columns=BACKUP_LEGACY_V1_TABLE_COLUMNS[table],
            )
            for table in BACKUP_LEGACY_V1_TABLE_COLUMNS
        }
        return {
            "users": legacy_tables["users"],
            "credentials": legacy_tables["credentials"],
            "hives": [],
            "agents": [
                {
                    **row,
                    "hive_id": None,
                    "can_spawn_subagents": 0,
                    "max_subagents": 0,
                    "issue_creation_enabled": 0,
                    "issue_kind": "issue",
                    "issue_rate_limit_per_hour": 0,
                    "issue_labels": "[]",
                }
                for row in legacy_tables["agents"]
            ],
            "tasks": [
                {
                    **row,
                    "hive_id": None,
                }
                for row in legacy_tables["tasks"]
            ],
            "tool_actions": self.legacy_backup_tool_action_rows(legacy_tables),
            "schedules": [
                {
                    **row,
                    "hive_id": None,
                }
                for row in legacy_tables["schedules"]
            ],
            "heartbeat_events": legacy_tables["heartbeat_events"],
            "audit_events": legacy_tables["audit_events"],
        }

    def validate_backup_bundle(
        self,
        bundle: Mapping[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        if bundle.get("format") != BACKUP_FORMAT:
            raise StoreValidationError(f"unsupported backup format: {bundle.get('format')!r}")
        format_version = bundle.get("format_version")
        if format_version not in BACKUP_SUPPORTED_FORMAT_VERSIONS:
            supported = ", ".join(str(version) for version in BACKUP_SUPPORTED_FORMAT_VERSIONS)
            raise StoreValidationError(
                "unsupported backup format version: "
                f"{format_version!r}; supported versions: {supported}"
            )
        tables = bundle.get("tables")
        if not isinstance(tables, Mapping):
            raise StoreValidationError("backup bundle tables must be a JSON object")

        if format_version == BACKUP_LEGACY_FORMAT_VERSION:
            tables = self.normalize_legacy_v1_backup_tables(tables)

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

    def _default_agent_provider_tool_actions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": f"{AGENT_PROVIDER_CREDENTIAL_ACTION_PREFIX}{provider_id}",
                "description": f"Broker credential access for the {provider_id} agent provider.",
                "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": True},
                "required_credential_action": f"{AGENT_PROVIDER_CREDENTIAL_ACTION_PREFIX}{provider_id}",
                "risk_level": "medium",
            }
            for provider_id in sorted(self.config.agent_providers)
            if provider_id not in CREDENTIAL_OPTIONAL_AGENT_PROVIDERS
        ]

    def _seed_default_tool_actions(self, conn: sqlite3.Connection) -> None:
        now = iso()
        conn.executemany(
            """
            INSERT OR IGNORE INTO tool_actions
            (name, description, input_schema, required_credential_action, risk_level, created_at, updated_at)
            VALUES (:name, :description, :input_schema, :required_credential_action, :risk_level, :created_at, :updated_at)
            """,
            [
                {
                    **action,
                    "input_schema": dumps(action["input_schema"]),
                    "created_at": now,
                    "updated_at": now,
                }
                for action in (*DEFAULT_TOOL_ACTIONS, *self._default_agent_provider_tool_actions())
            ],
        )
        legacy_schema = dumps({"type": "object", "properties": {}, "required": [], "additionalProperties": True})
        conn.executemany(
            """
            INSERT OR IGNORE INTO tool_actions
            (name, description, input_schema, required_credential_action, risk_level, created_at, updated_at)
            VALUES (:name, :description, :input_schema, :required_credential_action, :risk_level, :created_at, :updated_at)
            """,
            [
                {
                    "name": action,
                    "description": "Migrated legacy action.",
                    "input_schema": legacy_schema,
                    "required_credential_action": action,
                    "risk_level": "medium",
                    "created_at": now,
                    "updated_at": now,
                }
                for action in self._existing_action_names(conn)
            ],
        )

    def _existing_action_names(self, conn: sqlite3.Connection) -> list[str]:
        actions: set[str] = set()
        for row in conn.execute("SELECT allowed_actions, approval_required_actions FROM credentials"):
            for column in ("allowed_actions", "approval_required_actions"):
                actions.update(self._json_action_names(row[column]))
        for query in ("SELECT action FROM tasks", "SELECT action FROM schedules", "SELECT action FROM leases"):
            for row in conn.execute(query):
                try:
                    actions.add(self.normalize_action_name(str(row["action"])))
                except StoreError:
                    continue
        return sorted(actions)

    def _json_action_names(self, value: str | None) -> set[str]:
        try:
            raw_actions = loads(value, [])
        except (TypeError, ValueError):
            raw_actions = []
        if not isinstance(raw_actions, list):
            return set()
        actions: set[str] = set()
        for action in raw_actions:
            try:
                actions.add(self.normalize_action_name(str(action)))
            except StoreError:
                continue
        return actions

    def is_setup_complete(self) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def setup_admin(self, username: str, password: str) -> dict[str, Any]:
        normalized_username = username.strip().lower()
        if len(normalized_username) < 3:
            raise StoreError("username must be at least 3 characters")
        if sum(1 for character in password if not character.isspace()) < 12:
            raise StoreError("admin password must include at least 12 non-whitespace characters")
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
                (
                  id, name, provider, secret_ref, allowed_agents, allowed_actions,
                  approval_required_actions, max_ttl_seconds, require_intent,
                  agent_lease_limit, credential_lease_limit, credential_action_limit,
                  rate_limit_window_seconds, provider_token_budget, provider_cost_budget_cents,
                  metadata, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    None,
                    None,
                    None,
                    DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
                    None,
                    None,
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
            rows = conn.execute("SELECT * FROM agents ORDER BY created_at DESC").fetchall()
            return self.public_agents(conn, rows)

    def create_agent(self, data: dict[str, Any], *, actor_id: str = "user") -> dict[str, Any]:
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
        hive_id = data.get("hive_id") or None
        if issue_creation_enabled and hive_id is None:
            raise StoreError("issue creation agents require hive_id")
        row = {
            "id": f"agent_{secrets.token_urlsafe(8)}",
            "name": data["name"],
            "role": data["role"],
            "provider": provider,
            "model": data.get("model") or self.config.agent_provider(provider).model,
            "status": "idle",
            "system_prompt": data.get("system_prompt") or "",
            "hive_id": hive_id,
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
            public_row = self.public_agent(conn, row)
        self.audit(
            "agent.created",
            actor_id,
            row["id"],
            "allowed",
            "agent created",
            {
                "status": row["status"],
                "hive_id": row["hive_id"],
                "can_spawn_subagents": bool(row["can_spawn_subagents"]),
                "issue_creation_enabled": bool(row["issue_creation_enabled"]),
                "issue_kind": row["issue_kind"],
                "issue_rate_limit_per_hour": row["issue_rate_limit_per_hour"],
            },
        )
        return public_row

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = self.get_agent_row(conn, agent_id)
            return self.public_agent(conn, row)

    def update_agent_status(self, agent_id: str, status: str, *, actor_id: str = "user") -> dict[str, Any]:
        normalized_status = AGENT_STATUS_ALIASES.get(status.strip().lower(), status.strip().lower())
        if normalized_status not in AGENT_STATUS_VALUES:
            raise StoreValidationError(f"unsupported agent status: {status}")
        updated_at = iso()
        with self.connect() as conn:
            row = self.get_agent_row(conn, agent_id)
            conn.execute(AGENT_STATUS_UPDATE_SQL, (normalized_status, updated_at, agent_id))
            updated = self.public_agent(conn, {**dict(row), "status": normalized_status, "updated_at": updated_at})
        self.audit("agent.status.updated", actor_id, agent_id, "allowed", f"agent marked {normalized_status}", {"status": normalized_status})
        return updated

    def get_agent_row(self, conn: sqlite3.Connection, agent_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            raise StoreNotFoundError(f"unknown agent: {agent_id}")
        return row

    def public_agents(
        self,
        conn: sqlite3.Connection,
        rows: list[sqlite3.Row] | list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        agents = [dict(row) for row in rows]
        if not agents:
            return []
        agent_ids = [str(agent["id"]) for agent in agents]
        agent_ids_json = dumps(agent_ids)
        assigned_tasks_by_agent = {agent_id: [] for agent_id in agent_ids}
        for task_row in conn.execute(
            """
            SELECT tasks.assigned_agent_id, tasks.id, tasks.title, tasks.status, tasks.priority, tasks.updated_at
            FROM tasks
            JOIN json_each(?) AS requested_agents
              ON tasks.assigned_agent_id = requested_agents.value
            ORDER BY tasks.updated_at DESC, tasks.created_at DESC
            """,
            (agent_ids_json,),
        ):
            assigned_tasks_by_agent[str(task_row["assigned_agent_id"])].append(
                {
                    "id": task_row["id"],
                    "title": task_row["title"],
                    "status": task_row["status"],
                    "priority": task_row["priority"],
                    "updated_at": task_row["updated_at"],
                }
            )
        assigned_schedules_by_agent = {agent_id: [] for agent_id in agent_ids}
        for schedule_row in conn.execute(
            """
            SELECT schedules.assigned_agent_id, schedules.id, schedules.name, schedules.enabled, schedules.interval_seconds, schedules.next_run_at, schedules.task_title
            FROM schedules
            JOIN json_each(?) AS requested_agents
              ON schedules.assigned_agent_id = requested_agents.value
            ORDER BY schedules.updated_at DESC, schedules.created_at DESC
            """,
            (agent_ids_json,),
        ):
            assigned_schedules_by_agent[str(schedule_row["assigned_agent_id"])].append(
                {
                    "id": schedule_row["id"],
                    "name": schedule_row["name"],
                    "enabled": bool(schedule_row["enabled"]),
                    "interval_seconds": schedule_row["interval_seconds"],
                    "next_run_at": schedule_row["next_run_at"],
                    "task_title": schedule_row["task_title"],
                }
            )
        credential_policies_by_agent = {agent_id: [] for agent_id in agent_ids}
        for credential_row in conn.execute(
            """
            SELECT id, name, provider, allowed_agents, allowed_actions, max_ttl_seconds, require_intent
            FROM credentials
            ORDER BY created_at DESC
            """
        ):
            policy = {
                "id": credential_row["id"],
                "name": credential_row["name"],
                "provider": credential_row["provider"],
                "allowed_actions": loads(credential_row["allowed_actions"], []),
                "max_ttl_seconds": credential_row["max_ttl_seconds"],
                "require_intent": bool(credential_row["require_intent"]),
            }
            for allowed_agent_id in loads(credential_row["allowed_agents"], []):
                if allowed_agent_id in credential_policies_by_agent:
                    credential_policies_by_agent[allowed_agent_id].append(policy)
        return [
            self._build_public_agent(
                row=agent,
                assigned_tasks=assigned_tasks_by_agent[agent["id"]],
                assigned_schedules=assigned_schedules_by_agent[agent["id"]],
                credential_policies=credential_policies_by_agent[agent["id"]],
            )
            for agent in agents
        ]

    def public_agent(self, conn: sqlite3.Connection, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return self.public_agents(conn, [row])[0]

    def _build_public_agent(
        self,
        *,
        row: sqlite3.Row | dict[str, Any],
        assigned_tasks: list[dict[str, Any]],
        assigned_schedules: list[dict[str, Any]],
        credential_policies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        agent = dict(row)
        agent["status"] = AGENT_STATUS_ALIASES.get(str(agent["status"]).strip().lower(), agent["status"])
        agent["can_spawn_subagents"] = bool(agent["can_spawn_subagents"])
        agent["issue_creation_enabled"] = bool(agent["issue_creation_enabled"])
        agent["max_subagents"] = int(agent["max_subagents"])
        agent["issue_rate_limit_per_hour"] = int(agent["issue_rate_limit_per_hour"])
        agent["issue_labels"] = loads(agent["issue_labels"], [])
        agent["assigned_task_count"] = len(assigned_tasks)
        agent["active_task_count"] = sum(1 for task in assigned_tasks if task["status"] not in FINAL_TASK_STATUSES)
        agent["assigned_schedule_count"] = len(assigned_schedules)
        agent["credential_policy_count"] = len(credential_policies)
        agent["assigned_tasks"] = assigned_tasks
        agent["assigned_schedules"] = assigned_schedules
        agent["credential_policies"] = credential_policies
        return agent

    def _prepare_tool_action_row(self, data: dict[str, Any]) -> dict[str, Any]:
        now = iso()
        name = self.normalize_action_name(str(data["name"]))
        required_action = self.normalize_action_name(str(data.get("required_credential_action") or name))
        risk_level = str(data.get("risk_level") or "low").strip().lower()
        if risk_level not in TOOL_ACTION_RISK_LEVELS:
            raise StoreError("tool action risk_level must be low, medium, or high")
        try:
            schema = validate_tool_action_schema(data.get("input_schema") or {"type": "object"})
        except ValueError as exc:
            raise StoreError(str(exc)) from exc
        return {
            "name": name,
            "description": str(data.get("description") or "").strip(),
            "input_schema": dumps(schema),
            "required_credential_action": required_action,
            "risk_level": risk_level,
            "created_at": now,
            "updated_at": now,
        }

    def public_tool_action(self, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {
            "name": row["name"],
            "description": row["description"],
            "input_schema": loads(row["input_schema"], {}),
            "required_credential_action": row["required_credential_action"],
            "risk_level": row["risk_level"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_tool_actions(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [self.public_tool_action(row) for row in conn.execute("SELECT * FROM tool_actions ORDER BY name")]

    def get_tool_action(self, name: str) -> dict[str, Any]:
        normalized_name = self.normalize_action_name(str(name))
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tool_actions WHERE name = ?", (normalized_name,)).fetchone()
            if row is None:
                raise StoreNotFoundError(f"unknown tool action: {normalized_name}")
            return self.public_tool_action(row)

    def create_tool_action(self, data: dict[str, Any]) -> dict[str, Any]:
        row = self._prepare_tool_action_row(data)
        with self.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO tool_actions
                    (name, description, input_schema, required_credential_action, risk_level, created_at, updated_at)
                    VALUES (:name, :description, :input_schema, :required_credential_action, :risk_level, :created_at, :updated_at)
                    """,
                    row,
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"tool action already exists: {row['name']}") from exc
        return self.public_tool_action(row)

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

    def normalize_optional_positive_int(self, value: Any, *, field_name: str) -> int | None:
        if value is None or value == "":
            return None
        try:
            normalized = int(value)
        except (TypeError, ValueError) as exc:
            raise StoreError(f"{field_name} must be a positive integer") from exc
        if normalized < 1:
            raise StoreError(f"{field_name} must be at least 1")
        return normalized

    def normalize_positive_int(self, value: Any, *, field_name: str, default: int) -> int:
        normalized = self.normalize_optional_positive_int(default if value is None or value == "" else value, field_name=field_name)
        if normalized is None:
            raise StoreError(f"{field_name} must be at least 1")
        return normalized

    def _prepare_credential_row(
        self,
        data: dict[str, Any],
        *,
        allow_managed_secret_metadata: bool = False,
    ) -> dict[str, Any]:
        now = iso()
        actions, approval_required_actions = self.normalize_credential_action_policy(
            data["allowed_actions"],
            data.get("approval_required_actions") or [],
        )
        agents = sorted(set(agent.strip() for agent in (data.get("allowed_agents") or []) if agent.strip()))
        provider = str(data["provider"]).strip().lower()
        name = str(data["name"]).strip()
        secret_ref = str(data.get("secret_ref") or "").strip()
        metadata = self.normalize_credential_metadata(provider, data.get("metadata"))
        if not allow_managed_secret_metadata:
            try:
                validate_external_credential_metadata(metadata)
            except ValueError as exc:
                raise StoreError(str(exc)) from exc
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
            "agent_lease_limit": self.normalize_optional_positive_int(
                data.get("agent_lease_limit"),
                field_name="agent_lease_limit",
            ),
            "credential_lease_limit": self.normalize_optional_positive_int(
                data.get("credential_lease_limit"),
                field_name="credential_lease_limit",
            ),
            "credential_action_limit": self.normalize_optional_positive_int(
                data.get("credential_action_limit"),
                field_name="credential_action_limit",
            ),
            "rate_limit_window_seconds": self.normalize_positive_int(
                data.get("rate_limit_window_seconds"),
                field_name="rate_limit_window_seconds",
                default=DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
            ),
            "provider_token_budget": self.normalize_optional_positive_int(
                data.get("provider_token_budget"),
                field_name="provider_token_budget",
            ),
            "provider_cost_budget_cents": self.normalize_optional_positive_int(
                data.get("provider_cost_budget_cents"),
                field_name="provider_cost_budget_cents",
            ),
            "metadata": dumps(metadata),
            "created_at": now,
            "updated_at": now,
        }
        try:
            row["secret_ref"] = validate_secret_ref(row["secret_ref"])
        except ValueError as exc:
            raise StoreError(str(exc)) from exc
        return row

    def normalize_action_name(self, action: str) -> str:
        normalized = action.strip().lower()
        if not normalized:
            raise StoreError("action is required")
        if len(normalized) > 64 or SAFE_ACTION_NAME.fullmatch(normalized) is None:
            raise StoreError("actions must use lowercase snake_case names")
        return normalized

    def normalize_credential_action_policy(
        self,
        allowed_actions: Sequence[str],
        approval_required_actions: Sequence[str] | None = None,
    ) -> tuple[list[str], list[str]]:
        actions = sorted(set(self.normalize_action_name(action) for action in allowed_actions if action.strip()))
        approvals = sorted(
            set(self.normalize_action_name(action) for action in (approval_required_actions or []) if action.strip())
        )
        if not actions:
            raise StoreError("credential must allow at least one action")
        if not set(approvals).issubset(actions):
            raise StoreError("approval_required_actions must be a subset of allowed_actions")
        return actions, approvals

    def create_credential(self, data: dict[str, Any]) -> dict[str, Any]:
        row = self._prepare_credential_row(data)
        try:
            row["secret_ref"] = validate_external_secret_ref(row["secret_ref"])
        except ValueError as exc:
            raise StoreError(str(exc)) from exc
        with self.connect() as conn:
            self.validate_agent_scope(
                conn,
                field_name="allowed_agents",
                values=loads(row["allowed_agents"], []),
            )
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
            self.validate_agent_scope(
                conn,
                field_name="allowed_agents",
                values=loads(credential_row["allowed_agents"], []),
            )
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
            self.validate_agent_scope(
                conn,
                field_name="allowed_agents",
                values=loads(credential_row["allowed_agents"], []),
            )
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
            row = conn.execute(CREDENTIAL_BY_ID_QUERY, (credential_id,)).fetchone()
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
                "agent_lease_limit": row["agent_lease_limit"],
                "credential_lease_limit": row["credential_lease_limit"],
                "credential_action_limit": row["credential_action_limit"],
                "rate_limit_window_seconds": row["rate_limit_window_seconds"] or DEFAULT_RATE_LIMIT_WINDOW_SECONDS,
                "provider_token_budget": row["provider_token_budget"],
                "provider_cost_budget_cents": row["provider_cost_budget_cents"],
            },
            "metadata": redact_public_metadata_value(loads(row["metadata"], {})),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def credential_policy_limit(self, credential: sqlite3.Row | dict[str, Any], field_name: str) -> int | None:
        value = credential[field_name]
        return int(value) if value is not None else None

    def credential_rate_limit_window(self, credential: sqlite3.Row | dict[str, Any]) -> int:
        return int(credential["rate_limit_window_seconds"] or DEFAULT_RATE_LIMIT_WINDOW_SECONDS)

    def count_recent_audit_events(
        self,
        conn: sqlite3.Connection,
        *,
        event_types: tuple[str, ...],
        target_id: str,
        actor_id: str | None = None,
        window_seconds: int,
    ) -> int:
        since = iso(utcnow() - timedelta(seconds=window_seconds))
        if not event_types:
            raise StoreError("unsupported audit event count shape")
        query = "SELECT type, metadata FROM audit_events WHERE target_id = ? AND created_at >= ?"
        params: list[Any] = [target_id, since]
        if actor_id is not None:
            query += " AND actor_id = ?"
            params.append(actor_id)
        return sum(
            1
            for row in conn.execute(query, params)
            if row["type"] in event_types
            if self.audit_event_counts_toward_rate_limit(row["type"], row["metadata"])
        )

    def audit_event_counts_toward_rate_limit(self, event_type: str, metadata: str | None) -> bool:
        if event_type != LEASE_DENIED_EVENT:
            return True
        try:
            parsed_metadata = loads(metadata, {})
        except (TypeError, ValueError):
            return True
        return isinstance(parsed_metadata, dict) and parsed_metadata.get(LEASE_REQUEST_COUNTED_METADATA_KEY) is True

    def lease_request_rate_limit_denial(
        self,
        conn: sqlite3.Connection,
        *,
        credential: sqlite3.Row | dict[str, Any],
        agent_id: str,
    ) -> RateLimitDenial | None:
        window_seconds = self.credential_rate_limit_window(credential)
        agent_limit = self.credential_policy_limit(credential, "agent_lease_limit")
        if agent_limit is not None:
            count = self.count_recent_audit_events(
                conn,
                event_types=LEASE_REQUEST_RATE_LIMIT_EVENTS,
                target_id=credential["id"],
                actor_id=agent_id,
                window_seconds=window_seconds,
            )
            if count >= agent_limit:
                return RateLimitDenial(
                    reason="agent lease request rate limit exceeded",
                    metadata={
                        "rate_limit": "agent_lease_limit",
                        "limit": agent_limit,
                        "count": count,
                        "window_seconds": window_seconds,
                    },
                )
        credential_limit = self.credential_policy_limit(credential, "credential_lease_limit")
        if credential_limit is not None:
            count = self.count_recent_audit_events(
                conn,
                event_types=LEASE_REQUEST_RATE_LIMIT_EVENTS,
                target_id=credential["id"],
                window_seconds=window_seconds,
            )
            if count >= credential_limit:
                return RateLimitDenial(
                    reason="credential lease request rate limit exceeded",
                    metadata={
                        "rate_limit": "credential_lease_limit",
                        "limit": credential_limit,
                        "count": count,
                        "window_seconds": window_seconds,
                    },
                )
        return None

    def credential_action_rate_limit_denial(
        self,
        conn: sqlite3.Connection,
        *,
        lease: sqlite3.Row,
    ) -> RateLimitDenial | None:
        credential = conn.execute(CREDENTIAL_BY_ID_QUERY, (lease["credential_id"],)).fetchone()
        if credential is None:
            return None
        action_limit = self.credential_policy_limit(credential, "credential_action_limit")
        if action_limit is None:
            return None
        window_seconds = self.credential_rate_limit_window(credential)
        count = self.count_recent_audit_events(
            conn,
            event_types=ACTION_RATE_LIMIT_EVENTS,
            target_id=credential["id"],
            window_seconds=window_seconds,
        )
        if count < action_limit:
            return None
        return RateLimitDenial(
            reason="credential action rate limit exceeded",
            metadata={
                "rate_limit": "credential_action_limit",
                "limit": action_limit,
                "count": count,
                "window_seconds": window_seconds,
            },
        )

    def insert_lease_or_rate_limit_denial(
        self,
        *,
        row: dict[str, Any],
        credential: dict[str, Any],
        agent_id: str,
        credential_id: str,
        normalized_action: str,
        credential_action: str,
        ttl: int,
        requires_approval: bool,
        review_reason: str,
        base_audit_metadata: Mapping[str, Any],
    ) -> str | None:
        with self.connect() as conn:
            conn.execute(BEGIN_IMMEDIATE_SQL)
            denial = self.lease_request_rate_limit_denial(conn, credential=credential, agent_id=agent_id)
            if denial is not None:
                self._insert_audit(
                    conn,
                    LEASE_DENIED_EVENT,
                    agent_id,
                    credential_id,
                    "denied",
                    denial.reason,
                    {
                        **base_audit_metadata,
                        **self.audit_action_metadata(normalized_action),
                        "credential_action": credential_action,
                        **denial.metadata,
                    },
                )
                return denial.reason
            conn.execute(
                """
                INSERT INTO leases (id, token_hash, token_preview, credential_id, agent_id, action, intent, ttl_seconds, status, issued_at, expires_at)
                VALUES (:id, :token_hash, :token_preview, :credential_id, :agent_id, :action, :intent, :ttl_seconds, :status, :issued_at, :expires_at)
                """,
                row,
            )
            self._insert_audit(
                conn,
                "credential.lease.pending" if requires_approval else "credential.lease.issued",
                agent_id,
                credential_id,
                "pending" if requires_approval else "allowed",
                "action requires operator approval" if requires_approval else review_reason,
                {
                    **base_audit_metadata,
                    **self.audit_action_metadata(normalized_action, ttl_seconds=ttl),
                    "credential_action": credential_action,
                    "lease_id": row["id"],
                },
            )
        return None

    def record_lease_rate_limit_denial_if_limited(
        self,
        *,
        credential: dict[str, Any],
        agent_id: str,
        credential_id: str,
        normalized_action: str,
        credential_action: str,
        base_audit_metadata: Mapping[str, Any],
    ) -> str | None:
        with self.connect() as conn:
            conn.execute(BEGIN_IMMEDIATE_SQL)
            denial = self.lease_request_rate_limit_denial(conn, credential=credential, agent_id=agent_id)
            if denial is None:
                return None
            self._insert_audit(
                conn,
                LEASE_DENIED_EVENT,
                agent_id,
                credential_id,
                "denied",
                denial.reason,
                {
                    **base_audit_metadata,
                    **self.audit_action_metadata(normalized_action),
                    "credential_action": credential_action,
                    **denial.metadata,
                },
            )
            return denial.reason

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

    def _tool_action_for_request(self, action: str) -> dict[str, Any]:
        normalized_action = normalize_tool_action_name(action)
        if not normalized_action:
            raise StoreError("tool action is required")
        legacy_agent_provider_action = self._legacy_agent_provider_tool_action(normalized_action)
        if legacy_agent_provider_action is not None:
            return legacy_agent_provider_action
        if len(normalized_action) > 64 or SAFE_ACTION_NAME.fullmatch(normalized_action) is None:
            raise StoreError(f"unknown tool action: {self.audit_action_label(normalized_action)}")
        try:
            return self.get_tool_action(normalized_action)
        except StoreNotFoundError as exc:
            raise StoreError(f"unknown tool action: {self.audit_action_label(normalized_action)}") from exc

    def _legacy_agent_provider_tool_action(self, normalized_action: str) -> dict[str, Any] | None:
        if not normalized_action.startswith(LEGACY_AGENT_PROVIDER_CREDENTIAL_ACTION_PREFIX):
            return None
        provider = normalized_action.removeprefix(LEGACY_AGENT_PROVIDER_CREDENTIAL_ACTION_PREFIX)
        if not provider:
            return None
        provider_id = normalize_agent_provider_id(provider)
        if normalized_action != f"{LEGACY_AGENT_PROVIDER_CREDENTIAL_ACTION_PREFIX}{provider_id}":
            return None
        if provider_id not in self.config.agent_providers or SAFE_ACTION_NAME.fullmatch(provider_id) is None:
            return None
        canonical_action = f"{AGENT_PROVIDER_CREDENTIAL_ACTION_PREFIX}{provider_id}"
        try:
            action = self.get_tool_action(canonical_action)
        except StoreError:
            return None
        return {
            **action,
            "name": normalized_action,
            "required_credential_action": normalized_action,
        }

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
        normalized_action = normalize_tool_action_name(action)
        base_audit_metadata = dict(audit_metadata or {})
        credential_action = normalized_action

        def lease_audit_metadata(
            *,
            ttl_seconds: int | None = None,
            lease_id: str | None = None,
            credential_action_name: str | None = None,
        ) -> dict[str, Any]:
            metadata = {
                **base_audit_metadata,
                **self.audit_action_metadata(normalized_action, ttl_seconds=ttl_seconds),
            }
            if credential_action_name is not None:
                metadata["credential_action"] = credential_action_name
            if lease_id is not None:
                metadata["lease_id"] = lease_id
            return metadata

        try:
            with self.connect() as conn:
                self.get_agent_row(conn, agent_id)
        except StoreError as exc:
            self.audit(
                LEASE_DENIED_EVENT,
                agent_id,
                credential_id,
                "denied",
                str(exc),
                lease_audit_metadata(),
            )
            raise
        try:
            credential = self.get_credential(credential_id)
        except StoreError as exc:
            self.audit(
                LEASE_DENIED_EVENT,
                agent_id,
                credential_id,
                "denied",
                str(exc),
                lease_audit_metadata(),
            )
            raise
        try:
            tool_action = self._tool_action_for_request(action)
        except StoreError as exc:
            self.audit(
                LEASE_DENIED_EVENT,
                agent_id,
                credential_id,
                "denied",
                str(exc),
                lease_audit_metadata(),
            )
            raise
        normalized_action = tool_action["name"]
        credential_action = tool_action["required_credential_action"]
        approval_required_actions = set(loads(credential["approval_required_actions"], []))
        review_input = PolicyReviewInput(
            credential_id=credential_id,
            credential_provider=credential["provider"],
            allowed_agents=frozenset(loads(credential["allowed_agents"], [])),
            allowed_actions=frozenset(loads(credential["allowed_actions"], [])),
            require_intent=bool(credential["require_intent"]),
            agent_id=agent_id,
            action=credential_action,
            intent=intent,
            credential_metadata=loads(credential["metadata"], {}),
        )
        deterministic_review = self._policy_engine.review_deterministic_request(review_input)
        if not deterministic_review.allowed:
            self.audit(
                LEASE_DENIED_EVENT,
                agent_id,
                credential_id,
                "denied",
                deterministic_review.reason,
                lease_audit_metadata(credential_action_name=credential_action),
            )
            raise StoreError(deterministic_review.reason)
        error_detail = self.record_lease_rate_limit_denial_if_limited(
            credential=credential,
            agent_id=agent_id,
            credential_id=credential_id,
            normalized_action=normalized_action,
            credential_action=deterministic_review.normalized_action,
            base_audit_metadata=base_audit_metadata,
        )
        if error_detail is not None:
            raise StoreError(error_detail)

        review = self._policy_engine.review_request(review_input)
        if not review.allowed:
            metadata = lease_audit_metadata(credential_action_name=credential_action)
            metadata[LEASE_REQUEST_COUNTED_METADATA_KEY] = True
            self.audit(
                LEASE_DENIED_EVENT,
                agent_id,
                credential_id,
                "denied",
                review.reason,
                metadata,
            )
            raise StoreError(review.reason)
        ttl = min(int(ttl_seconds or credential["max_ttl_seconds"]), int(credential["max_ttl_seconds"]))
        requires_approval = credential_action in approval_required_actions
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
        error_detail = self.insert_lease_or_rate_limit_denial(
            row=row,
            credential=credential,
            agent_id=agent_id,
            credential_id=credential_id,
            normalized_action=normalized_action,
            credential_action=review.normalized_action,
            ttl=ttl,
            requires_approval=requires_approval,
            review_reason=review.reason,
            base_audit_metadata=base_audit_metadata,
        )
        if error_detail is not None:
            raise StoreError(error_detail)
        if requires_approval:
            return None, self.public_lease(row)
        public = self.public_lease(row)
        public["lease_token"] = token
        return token, public

    def perform_credential_action(
        self,
        lease_token: str,
        action: str,
        payload: dict[str, Any],
        *,
        validate_payload: bool = True,
    ) -> dict[str, Any]:
        token_hash = self.hash_token(lease_token)
        normalized_action = normalize_tool_action_name(action)
        payload_key_count = len(payload)
        error_detail: str | None = None
        result: dict[str, Any] | None = None
        with self.connect() as conn:
            conn.execute(BEGIN_IMMEDIATE_SQL)
            lease = conn.execute("SELECT * FROM leases WHERE token_hash = ?", (token_hash,)).fetchone()
            if lease is None:
                error_detail = "unknown credential lease token"
                self._insert_unknown_credential_action_denial(
                    conn,
                    normalized_action,
                    error_detail,
                    payload_key_count,
                )
            else:
                error_detail = self._preflight_credential_action(
                    conn,
                    lease,
                    normalized_action,
                    payload,
                    payload_key_count,
                    validate_payload=validate_payload,
                )
                if error_detail is None:
                    denial = self.credential_action_rate_limit_denial(conn, lease=lease)
                    if denial is not None:
                        error_detail = denial.reason
                        self._insert_credential_action_denial(
                            conn,
                            lease,
                            normalized_action,
                            denial.reason,
                            payload_key_count,
                            metadata=denial.metadata,
                        )
                    else:
                        result, error_detail = self._consume_credential_action(
                            conn,
                            lease,
                            normalized_action,
                            payload,
                        )
        if error_detail is not None:
            raise StoreError(error_detail)
        if result is None:
            raise RuntimeError("credential action flow ended without a result")
        return result

    def _preflight_credential_action(
        self,
        conn: sqlite3.Connection,
        lease: sqlite3.Row,
        normalized_action: str,
        payload: dict[str, Any],
        payload_key_count: int,
        *,
        validate_payload: bool,
    ) -> str | None:
        error_detail = self._credential_action_denial_reason(lease, normalized_action)
        if error_detail is not None:
            self._insert_credential_action_denial(conn, lease, normalized_action, error_detail, payload_key_count)
            return error_detail
        try:
            tool_action = self._tool_action_for_request(normalized_action)
        except StoreError as exc:
            error_detail = str(exc)
            self._insert_credential_action_denial(conn, lease, normalized_action, error_detail, payload_key_count)
            return error_detail
        payload_error = payload_schema_error(tool_action["input_schema"], payload) if validate_payload else None
        if payload_error is None:
            return None
        self._insert_credential_action_denial(
            conn,
            lease,
            normalized_action,
            payload_error,
            payload_key_count,
        )
        return payload_error

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
        payload_key_count: int,
    ) -> None:
        self._insert_audit(
            conn,
            ACTION_DENIED_EVENT,
            "unknown",
            "credential_lease",
            "denied",
            error_detail,
            self.audit_action_metadata(normalized_action, payload_key_count=payload_key_count),
        )

    def credential_action_denial_metadata(
        self,
        lease: sqlite3.Row,
        normalized_action: str,
        payload_key_count: int,
    ) -> dict[str, Any]:
        metadata = self.audit_action_metadata(normalized_action, payload_key_count=payload_key_count)
        metadata["lease_id"] = lease["id"]
        if lease["status"] in {"pending", "denied"}:
            metadata["lease_status"] = lease["status"]
        return metadata

    def _insert_credential_action_denial(
        self,
        conn: sqlite3.Connection,
        lease: sqlite3.Row,
        normalized_action: str,
        error_detail: str,
        payload_key_count: int,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        audit_metadata = self.credential_action_denial_metadata(lease, normalized_action, payload_key_count)
        audit_metadata.update(metadata or {})
        self._insert_audit(
            conn,
            ACTION_DENIED_EVENT,
            lease["agent_id"],
            lease["credential_id"],
            "denied",
            error_detail,
            audit_metadata,
        )

    def _consume_credential_action(
        self,
        conn: sqlite3.Connection,
        lease: sqlite3.Row,
        normalized_action: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        payload_key_count = len(payload)
        credential = conn.execute("SELECT * FROM credentials WHERE id = ?", (lease["credential_id"],)).fetchone()
        if credential is None:
            error_detail = "credential no longer exists"
            self._insert_credential_action_denial(conn, lease, normalized_action, error_detail, payload_key_count)
            return None, error_detail
        try:
            tool_action = self._tool_action_for_request(normalized_action)
        except StoreError as exc:
            error_detail = str(exc)
            self._insert_credential_action_denial(conn, lease, normalized_action, error_detail, payload_key_count)
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
            self._insert_credential_action_denial(conn, lease, normalized_action, error_detail, payload_key_count)
            return None, error_detail
        self._insert_audit(
            conn,
            "credential.action.performed",
            lease["agent_id"],
            lease["credential_id"],
            "allowed",
            "action matched active credential lease",
            self.audit_action_metadata(normalized_action, payload_key_count=payload_key_count),
        )
        return (
            {
                "ok": True,
                "provider": credential["provider"],
                "credential_id": credential["id"],
                "action": normalized_action,
                "credential_action": tool_action["required_credential_action"],
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
        approval_metadata = self.audit_action_metadata(updated["action"], ttl_seconds=updated["ttl_seconds"])
        approval_metadata["agent_id"] = updated["agent_id"]
        approval_metadata["lease_id"] = updated["id"]
        self.audit(
            "credential.lease.approved",
            actor_id,
            updated["credential_id"],
            "allowed",
            "operator approved lease request",
            approval_metadata,
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
        denial_metadata = self.audit_action_metadata(updated["action"], ttl_seconds=updated["ttl_seconds"])
        denial_metadata["agent_id"] = updated["agent_id"]
        denial_metadata["lease_id"] = updated["id"]
        self.audit(
            LEASE_DENIED_EVENT,
            actor_id,
            updated["credential_id"],
            "denied",
            "operator denied lease request",
            denial_metadata,
        )
        return self.public_lease(updated)

    def hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def list_leases(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [self.public_lease(row) for row in conn.execute("SELECT * FROM leases ORDER BY issued_at DESC")]

    def ping(self) -> None:
        with self.connect() as conn:
            conn.execute("SELECT 1").fetchone()

    def runtime_overview(self, *, limit: int = 5) -> dict[str, Any]:
        now = utcnow()
        now_iso = iso(now)
        terminal_statuses = tuple(sorted(TERMINAL_TASK_STATUS_VALUES))
        with self.connect() as conn:
            active_leases = conn.execute(
                "SELECT COUNT(*) FROM leases WHERE status = 'active' AND expires_at > ?",
                (now_iso,),
            ).fetchone()[0]
            due_schedule_count = conn.execute(
                "SELECT COUNT(*) FROM schedules WHERE enabled = 1 AND next_run_at <= ?",
                (now_iso,),
            ).fetchone()[0]
            due_schedule_ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM schedules WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at ASC",
                    (now_iso,),
                )
            ]
            due_schedule_rows = list(
                conn.execute(
                    "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at ASC LIMIT ?",
                    (now_iso, limit),
                )
            )
            stale_heartbeat_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM tasks
                WHERE next_heartbeat_at IS NOT NULL
                  AND next_heartbeat_at <= ?
                  AND lower(status) NOT IN (?, ?, ?)
                """,
                (now_iso, *terminal_statuses),
            ).fetchone()[0]
            stale_heartbeat_task_ids = [
                row["id"]
                for row in conn.execute(
                    """
                    SELECT id
                    FROM tasks
                    WHERE next_heartbeat_at IS NOT NULL
                      AND next_heartbeat_at <= ?
                      AND lower(status) NOT IN (?, ?, ?)
                    ORDER BY next_heartbeat_at ASC
                    """,
                    (now_iso, *terminal_statuses),
                )
            ]
            stale_heartbeat_rows = list(
                conn.execute(
                    """
                    SELECT *
                    FROM tasks
                    WHERE next_heartbeat_at IS NOT NULL
                      AND next_heartbeat_at <= ?
                      AND lower(status) NOT IN (?, ?, ?)
                    ORDER BY next_heartbeat_at ASC
                    LIMIT ?
                    """,
                    (now_iso, *terminal_statuses, limit),
                )
            )
            failed_task_count = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE lower(status) = ?",
                ("failed",),
            ).fetchone()[0]
            failed_task_rows = list(
                conn.execute(
                    "SELECT * FROM tasks WHERE lower(status) = ? ORDER BY updated_at DESC LIMIT ?",
                    ("failed", limit),
                )
            )
            stale_heartbeats = [self.runtime_task_view(conn, row, "next_heartbeat_at", now) for row in stale_heartbeat_rows]
            failed_tasks = [self.runtime_task_view(conn, row, "updated_at", now) for row in failed_task_rows]
        return {
            "checked_at": now_iso,
            "counts": {
                "active_leases": active_leases,
                "due_schedules": due_schedule_count,
                "stale_heartbeats": stale_heartbeat_count,
                "failed_tasks": failed_task_count,
            },
            "due_schedule_ids": due_schedule_ids,
            "stale_heartbeat_task_ids": stale_heartbeat_task_ids,
            "due_schedules": [self.runtime_schedule_view(row, now) for row in due_schedule_rows],
            "stale_heartbeats": stale_heartbeats,
            "failed_tasks": failed_tasks,
        }

    def overdue_seconds(self, timestamp: str | None, now: datetime) -> int:
        due_at = parse_dt(timestamp)
        if due_at is None:
            return 0
        if due_at.tzinfo is None or due_at.utcoffset() is None:
            due_at = due_at.replace(tzinfo=timezone.utc)
        return max(int((now - due_at).total_seconds()), 0)

    def runtime_schedule_view(self, row: sqlite3.Row | dict[str, Any], now: datetime) -> dict[str, Any]:
        schedule = self.public_schedule(row)
        schedule["overdue_seconds"] = self.overdue_seconds(schedule["next_run_at"], now)
        return schedule

    def runtime_task_view(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row | dict[str, Any],
        timestamp_key: str,
        now: datetime,
    ) -> dict[str, Any]:
        task = self.public_task(conn, row, now=now)
        task["overdue_seconds"] = self.overdue_seconds(task.get(timestamp_key), now)
        return task

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

    def audit_action_metadata(
        self,
        action: str,
        *,
        ttl_seconds: int | None = None,
        payload_key_count: int | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {"action": self.audit_action_label(action)}
        if ttl_seconds is not None:
            metadata["ttl_seconds"] = ttl_seconds
        if payload_key_count is not None:
            metadata["payload_key_count"] = payload_key_count
        return metadata

    def audit_action_label(self, action: str) -> str:
        normalized = action.strip().lower()
        if normalized and len(normalized) <= 64 and SAFE_ACTION_NAME.fullmatch(normalized):
            return normalized
        return "<redacted>"

    def heartbeat_audit_metadata(self, note: str) -> dict[str, Any]:
        normalized_note = note.strip()
        return {
            "note_present": bool(normalized_note),
            "note_length": len(normalized_note),
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

    def public_task(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row | dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        item = dict(row)
        last_heartbeat_at = self.task_last_heartbeat_at(conn, item)
        heartbeat_state, heartbeat_overdue_seconds = self.task_heartbeat_state(
            item,
            last_heartbeat_at,
            now=now,
        )
        item["last_heartbeat_at"] = last_heartbeat_at
        item["heartbeat_state"] = heartbeat_state
        item["heartbeat_overdue_seconds"] = heartbeat_overdue_seconds
        return item

    def task_last_heartbeat_at(self, conn: sqlite3.Connection, item: dict[str, Any]) -> str | None:
        if "last_heartbeat_at" in item:
            return item["last_heartbeat_at"]
        last_heartbeat_row = conn.execute(
            "SELECT created_at FROM heartbeat_events WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
            (item["id"],),
        ).fetchone()
        return last_heartbeat_row["created_at"] if last_heartbeat_row else None

    def task_heartbeat_state(
        self,
        item: dict[str, Any],
        last_heartbeat_at: str | None,
        *,
        now: datetime | None = None,
    ) -> tuple[str, int | None]:
        if not item["heartbeat_seconds"] or item["status"] in TERMINAL_TASK_STATUS_VALUES:
            return ("disabled", None)
        deadline = parse_dt(item["next_heartbeat_at"])
        if deadline is None:
            return ("healthy", None)
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        elapsed_seconds = ((now or utcnow()) - deadline).total_seconds()
        if elapsed_seconds < 0:
            return ("healthy", None)
        return ("stale" if last_heartbeat_at else "missing", int(elapsed_seconds))

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

    def validate_agent_scope(
        self,
        conn: sqlite3.Connection,
        *,
        field_name: str,
        values: list[str],
    ) -> None:
        for value in values:
            self.validate_optional_agent_reference(conn, field_name=field_name, value=value)

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

    def validate_agent_credential_binding(
        self,
        conn: sqlite3.Connection,
        *,
        assigned_agent_id: str | None,
        credential_id: str | None,
    ) -> None:
        if assigned_agent_id is None or credential_id is None:
            return
        row = conn.execute("SELECT allowed_agents FROM credentials WHERE id = ?", (credential_id,)).fetchone()
        if row is None:
            raise StoreValidationError(f"credential_id references unknown credential: {credential_id}")
        allowed_agents = set(loads(row["allowed_agents"], []))
        if assigned_agent_id not in allowed_agents:
            raise StoreValidationError(
                f"assigned_agent_id is not allowed to use credential {credential_id}: {assigned_agent_id}"
            )

    def validate_optional_tool_action_reference(
        self,
        conn: sqlite3.Connection,
        *,
        field_name: str,
        value: str | None,
    ) -> str:
        if not value or not str(value).strip():
            return ""
        normalized = self.normalize_action_name(str(value))
        row = conn.execute("SELECT 1 FROM tool_actions WHERE name = ?", (normalized,)).fetchone()
        if row is None:
            raise StoreValidationError(f"{field_name} references unknown tool action: {normalized}")
        return normalized

    def validate_task_priority(self, priority: str) -> None:
        if priority not in VALID_TASK_PRIORITIES:
            choices = ", ".join(sorted(VALID_TASK_PRIORITIES))
            raise StoreValidationError(f"priority must be one of: {choices}")

    def validate_task_status(self, status: str) -> None:
        if status not in VALID_TASK_STATUSES:
            choices = ", ".join(sorted(VALID_TASK_STATUSES))
            raise StoreValidationError(f"status must be one of: {choices}")

    def validate_initial_task_status(self, status: str) -> None:
        if status not in VALID_INITIAL_TASK_STATUSES:
            choices = ", ".join(sorted(VALID_INITIAL_TASK_STATUSES))
            raise StoreValidationError(f"new tasks must start in one of: {choices}")

    def validate_task_transition(self, current_status: str, next_status: str) -> None:
        if current_status == next_status:
            return
        allowed_statuses = VALID_TASK_STATUS_TRANSITIONS.get(current_status)
        if allowed_statuses is None or next_status not in allowed_statuses:
            raise StoreValidationError(f"cannot transition task from {current_status} to {next_status}")

    def validate_task_update_data(self, conn: sqlite3.Connection, data: dict[str, Any]) -> None:
        if "priority" in data:
            if data["priority"] is None:
                raise StoreValidationError("priority must not be null")
            self.validate_task_priority(str(data["priority"]))
        if "assigned_agent_id" in data:
            self.validate_optional_agent_reference(
                conn,
                field_name="assigned_agent_id",
                value=data["assigned_agent_id"] or None,
            )
        if "credential_id" in data:
            self.validate_optional_credential_reference(
                conn,
                field_name="credential_id",
                value=data["credential_id"] or None,
            )
        if "action" in data:
            data["action"] = self.validate_optional_tool_action_reference(
                conn,
                field_name="action",
                value=data["action"] or None,
            )

    def normalize_task_update_value(self, field: str, value: Any) -> Any:
        if field == "title" and value is None:
            raise StoreValidationError("title must not be null")
        if field in {"description", "action", "intent"} and value is None:
            return ""
        if field in {"assigned_agent_id", "credential_id"}:
            return value or None
        return value

    def apply_task_update_field(
        self,
        row: sqlite3.Row,
        updated: dict[str, Any],
        *,
        field: str,
        next_value: Any,
        now: datetime,
    ) -> bool:
        if field == "heartbeat_seconds":
            if next_value == row[field]:
                return False
            updated[field] = next_value
            updated["next_heartbeat_at"] = iso(now + timedelta(seconds=int(next_value))) if next_value else None
            return True
        if next_value == row[field]:
            return False
        updated[field] = next_value
        return True

    def normalize_schedule_priority(
        self,
        conn: sqlite3.Connection,
        schedule_row: sqlite3.Row,
        *,
        actor_id: str,
        now: datetime,
    ) -> str:
        priority = str(schedule_row["priority"] or "")
        if priority in VALID_TASK_PRIORITIES:
            return priority
        normalized = TaskPriority.NORMAL.value
        conn.execute(
            "UPDATE schedules SET priority = ?, updated_at = ? WHERE id = ?",
            (normalized, iso(now), schedule_row["id"]),
        )
        self._insert_audit(
            conn,
            "schedule.priority.normalized",
            actor_id,
            str(schedule_row["id"]),
            "allowed",
            "legacy schedule priority normalized",
            {"from_priority": priority, "to_priority": normalized},
            now=now,
        )
        return normalized

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
        self.validate_task_status(str(row["status"]))
        self.validate_initial_task_status(str(row["status"]))
        self.validate_task_priority(str(row["priority"]))
        self.validate_optional_credential_reference(
            conn,
            field_name="credential_id",
            value=row["credential_id"],
        )
        self.validate_agent_credential_binding(
            conn,
            assigned_agent_id=row["assigned_agent_id"],
            credential_id=row["credential_id"],
        )
        row["action"] = self.validate_optional_tool_action_reference(
            conn,
            field_name="action",
            value=row["action"],
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

    def _insert_task(
        self,
        conn: sqlite3.Connection,
        data: dict[str, Any],
        *,
        actor_id: str = "system",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        task_time = now or utcnow()
        row = self.prepare_task_row(conn, data, now=task_time)
        self.insert_task_row(conn, row)
        self._insert_audit(
            conn,
            "task.created",
            actor_id,
            row["id"],
            "allowed",
            "task created",
            {
                "status": row["status"],
                "priority": row["priority"],
                "hive_id": row["hive_id"],
                "assigned_agent_id": row["assigned_agent_id"],
                "credential_id": row["credential_id"],
                "action": row["action"],
                "intent": row["intent"],
                "heartbeat_seconds": row["heartbeat_seconds"],
            },
            now=task_time,
        )
        return self.public_task(conn, row, now=task_time)

    def create_task(self, data: dict[str, Any], *, actor_id: str = "system") -> dict[str, Any]:
        with self.connect() as conn:
            return self._insert_task(conn, data, actor_id=actor_id)

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

    def get_issue_request_agent_row(self, conn: sqlite3.Connection, agent_id: str) -> sqlite3.Row:
        row = conn.execute(AGENT_BY_ID_QUERY, (agent_id,)).fetchone()
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
        label_intent = f" Requested labels JSON: {dumps(merged_labels)}."
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
                    f"{label_intent}"
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
                agent = self.get_issue_request_agent_row(conn, agent_id)
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
            rows = conn.execute(
                """
                SELECT tasks.*, latest_heartbeats.last_heartbeat_at
                FROM tasks
                LEFT JOIN (
                    SELECT task_id, MAX(created_at) AS last_heartbeat_at
                    FROM heartbeat_events
                    GROUP BY task_id
                ) AS latest_heartbeats ON latest_heartbeats.task_id = tasks.id
                ORDER BY tasks.created_at DESC
                """
            ).fetchall()
            return [self.public_task(conn, row) for row in rows]

    def update_task(self, task_id: str, data: dict[str, Any], *, actor_id: str) -> dict[str, Any]:
        now = utcnow()
        with self.connect() as conn:
            row = self.get_task_row(conn, task_id)
            requested_fields = tuple(field for field in EDITABLE_TASK_FIELDS if field in data)
            if not requested_fields:
                raise StoreValidationError("task update requires at least one editable field")
            updated = dict(row)
            changes: list[str] = []
            self.validate_task_update_data(conn, data)

            for field in requested_fields:
                next_value = self.normalize_task_update_value(field, data[field])
                if self.apply_task_update_field(row, updated, field=field, next_value=next_value, now=now):
                    changes.append(field)

            if not changes:
                return self.public_task(conn, row, now=now)

            if {"assigned_agent_id", "credential_id"} & set(changes):
                self.validate_agent_credential_binding(
                    conn,
                    assigned_agent_id=updated["assigned_agent_id"],
                    credential_id=updated["credential_id"],
                )
            if str(updated["status"]) in TERMINAL_TASK_STATUS_VALUES:
                updated["next_heartbeat_at"] = None
            updated["updated_at"] = iso(now)
            conn.execute(
                """
                UPDATE tasks
                SET title = ?, description = ?, priority = ?, assigned_agent_id = ?, credential_id = ?, action = ?, intent = ?, heartbeat_seconds = ?, next_heartbeat_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated["title"],
                    updated["description"],
                    updated["priority"],
                    updated["assigned_agent_id"],
                    updated["credential_id"],
                    updated["action"],
                    updated["intent"],
                    updated["heartbeat_seconds"],
                    updated["next_heartbeat_at"],
                    updated["updated_at"],
                    task_id,
                ),
            )
        self.audit(
            "task.updated",
            actor_id,
            task_id,
            "allowed",
            "task details updated",
            {"fields": changes},
        )
        return self.get_task(task_id)

    def update_task_status(self, task_id: str, status: str, *, actor_id: str) -> dict[str, Any]:
        now_dt = utcnow()
        now = iso(now_dt)
        next_status = str(status)
        self.validate_task_status(next_status)
        with self.connect() as conn:
            row = self.get_task_row(conn, task_id)
            current_status = str(row["status"])
            self.validate_task_transition(current_status, next_status)
            if current_status == next_status:
                return self.public_task(conn, row, now=now_dt)
            if next_status in TERMINAL_TASK_STATUS_VALUES:
                next_heartbeat_at = None
            elif row["heartbeat_seconds"] and row["next_heartbeat_at"] is None:
                next_heartbeat_at = iso(now_dt + timedelta(seconds=int(row["heartbeat_seconds"])))
            else:
                next_heartbeat_at = row["next_heartbeat_at"]
            conn.execute(
                "UPDATE tasks SET status = ?, next_heartbeat_at = ?, updated_at = ? WHERE id = ?",
                (next_status, next_heartbeat_at, now, task_id),
            )
        self.audit(
            "task.status.updated",
            actor_id,
            task_id,
            "allowed",
            f"task marked {next_status}",
            {"from_status": current_status, "to_status": next_status},
        )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            return self.public_task(conn, self.get_task_row(conn, task_id))

    def run_task(self, task_id: str, operator_input: str | None = None) -> dict[str, Any]:
        with self.connect() as conn:
            task = dict(self.get_task_row(conn, task_id))
            if not task["assigned_agent_id"]:
                raise StoreValidationError("task must be assigned to an agent before execution")
            if task["status"] != "queued":
                raise StoreError("only queued tasks can be executed")
            agent = conn.execute(AGENT_BY_ID_QUERY, (task["assigned_agent_id"],)).fetchone()
            if agent is None:
                raise StoreValidationError(f"assigned_agent_id references unknown agent: {task['assigned_agent_id']}")
            agent = dict(agent)
            provider_id = normalize_agent_provider_id(agent["provider"])
            provider_config = self.config.agent_provider(provider_id)
            model = agent["model"] or provider_config.model
            now = iso()
            self.claim_queued_task_for_execution(conn, task_id, now)
            conn.execute(AGENT_STATUS_UPDATE_SQL, ("running", now, agent["id"]))
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
        action = self.agent_provider_credential_action(credential_id=credential_id, provider_id=provider_id)
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
                validate_payload=False,
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

    def agent_provider_credential_action(self, *, credential_id: str, provider_id: str) -> str:
        action = f"{AGENT_PROVIDER_CREDENTIAL_ACTION_PREFIX}{provider_id}"
        legacy_action = f"{LEGACY_AGENT_PROVIDER_CREDENTIAL_ACTION_PREFIX}{provider_id}"
        try:
            credential = self.get_credential(credential_id)
        except StoreError:
            return action
        allowed_actions = set(loads(credential["allowed_actions"], []))
        if action not in allowed_actions and legacy_action in allowed_actions:
            return legacy_action
        return action

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
                validate_payload=False,
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
            conn.execute(
                "UPDATE tasks SET status = ?, next_heartbeat_at = NULL, updated_at = ? WHERE id = ?",
                (status, now, task_id),
            )
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
                    "UPDATE agents SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                    ("idle", now, agent_id, "running"),
                )
        event_type = "task.execution.completed" if status == "done" else "task.execution.failed"
        self.audit(event_type, agent_id, task_id, decision, reason, metadata)

    def record_heartbeat(self, task_id: str, agent_id: str | None, note: str, *, actor_id: str = "system") -> dict[str, Any]:
        now = utcnow()
        provided_agent_id = agent_id or None
        audit_metadata = self.heartbeat_audit_metadata(note)
        try:
            with self.connect() as conn:
                task = self.get_task_row(conn, task_id)
                if task["status"] in TERMINAL_TASK_STATUS_VALUES:
                    raise StoreValidationError(f"cannot record heartbeat for task in terminal status: {task['status']}")
                self.validate_optional_agent_reference(
                    conn,
                    field_name="agent_id",
                    value=provided_agent_id,
                )
                assigned_agent_id = task["assigned_agent_id"]
                if assigned_agent_id and provided_agent_id and provided_agent_id != assigned_agent_id:
                    raise StoreValidationError(
                        f"agent_id does not match assigned agent for task {task_id}: {provided_agent_id}"
                    )
                next_heartbeat = None
                if task["heartbeat_seconds"]:
                    next_heartbeat = iso(now + timedelta(seconds=int(task["heartbeat_seconds"])))
                event = {
                    "id": f"hb_{secrets.token_urlsafe(10)}",
                    "task_id": task_id,
                    "agent_id": provided_agent_id or assigned_agent_id,
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
        except StoreError as exc:
            self.audit("task.heartbeat.denied", actor_id, task_id, "denied", str(exc), audit_metadata)
            raise
        self.audit(
            "task.heartbeat",
            actor_id,
            task_id,
            "allowed",
            "heartbeat recorded",
            self.heartbeat_audit_metadata(note),
        )
        return event

    def list_heartbeats(self, task_id: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if task_id:
                rows = conn.execute("SELECT * FROM heartbeat_events WHERE task_id = ? ORDER BY created_at DESC", (task_id,))
            else:
                rows = conn.execute("SELECT * FROM heartbeat_events ORDER BY created_at DESC")
            return [dict(row) for row in rows]

    def create_schedule(self, data: dict[str, Any], *, actor_id: str = "system") -> dict[str, Any]:
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
            self.validate_task_priority(str(row["priority"]))
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
            self.validate_agent_credential_binding(
                conn,
                assigned_agent_id=row["assigned_agent_id"],
                credential_id=row["credential_id"],
            )
            row["action"] = self.validate_optional_tool_action_reference(
                conn,
                field_name="action",
                value=row["action"],
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
            actor_id,
            row["id"],
            "allowed",
            "schedule created",
            {
                "interval_seconds": interval,
                "catch_up_policy": catch_up_policy,
                "priority": row["priority"],
                "hive_id": row["hive_id"],
                "assigned_agent_id": row["assigned_agent_id"],
                "credential_id": row["credential_id"],
                "action": row["action"],
                "intent": row["intent"],
                "enabled": bool(row["enabled"]),
            },
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

    def run_due_schedules_once(self, *, actor_id: str = "scheduler") -> list[dict[str, Any]]:
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
                    priority = self.normalize_schedule_priority(conn, row, actor_id=actor_id, now=now)
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
                                "priority": priority,
                                "hive_id": row["hive_id"],
                                "assigned_agent_id": row["assigned_agent_id"],
                                "credential_id": row["credential_id"],
                                "action": row["action"],
                                "intent": row["intent"],
                                "heartbeat_seconds": None,
                            },
                            actor_id=actor_id,
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
                        actor_id,
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
        sanitized_metadata = sanitize_log_value("metadata", metadata)
        row = {
            "id": f"audit_{secrets.token_urlsafe(10)}",
            "type": event_type,
            "actor_id": actor_id,
            "target_id": target_id,
            "decision": decision,
            "reason": reason,
            "metadata": dumps(sanitized_metadata),
            "created_at": iso(audit_time),
        }
        conn.execute(
            "INSERT INTO audit_events (id, type, actor_id, target_id, decision, reason, metadata, created_at) VALUES (:id, :type, :actor_id, :target_id, :decision, :reason, :metadata, :created_at)",
            row,
        )
        if should_emit_structured_audit_log(event_type):
            AUDIT_LOGGER.info(
                dumps(
                    {
                        "event": "audit.decision",
                        "type": event_type,
                        "actor_id": actor_id,
                        "target_id": target_id,
                        "decision": decision,
                        "reason": reason,
                        "metadata": sanitized_metadata,
                        "created_at": row["created_at"],
                    }
                )
            )
