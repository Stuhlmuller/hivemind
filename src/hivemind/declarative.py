from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
import sqlite3
from typing import Any

from hivemind.secret_refs import (
    BROKER_SECRET_REF_SCHEME,
    validate_external_credential_metadata,
    validate_external_secret_ref,
)
from hivemind.store import (
    SCHEDULE_CATCH_UP_POLICIES,
    HivemindStore,
    StoreError,
    dumps,
    iso,
    loads,
    parse_dt,
    utcnow,
)


CONFIG_VERSION = 1
CONFIG_TARGET_ID = "declarative-config"
OAUTH_REF_SCHEME = "oauth"
OAUTH_REF_ERROR = "oauth:// refs are broker-generated; reconnect OAuth credentials after import"
FORBIDDEN_METADATA_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "client_secret",
    "password",
    "private_key",
    "privatekey",
    "refresh_token",
    "secret",
    "token",
}
COMPACT_FORBIDDEN_METADATA_KEYS = {
    "".join(character for character in item if character.isalnum()) for item in FORBIDDEN_METADATA_KEYS
}
FORBIDDEN_METADATA_FRAGMENTS = ("secret", "password", "token")
EXISTING_ID_QUERIES = {
    "agents": "SELECT id FROM agents",
    "credentials": "SELECT id FROM credentials",
    "schedules": "SELECT id FROM schedules",
}
ROW_EXISTS_QUERIES = {
    "agents": "SELECT 1 FROM agents WHERE id = ?",
    "credentials": "SELECT 1 FROM credentials WHERE id = ?",
    "schedules": "SELECT 1 FROM schedules WHERE id = ?",
}


class DeclarativeConfigError(StoreError):
    pass


def export_declarative_config(store: HivemindStore) -> dict[str, Any]:
    with store.connect() as conn:
        agents = [
            _agent_config(row)
            for row in conn.execute(
                """
                SELECT id, name, role, provider, model, system_prompt
                FROM agents
                ORDER BY id
                """
            )
        ]
        credential_rows = list(
            conn.execute(
                """
                SELECT id, name, provider, secret_ref, allowed_agents, allowed_actions,
                       approval_required_actions, max_ttl_seconds, require_intent, metadata
                FROM credentials
                ORDER BY id
                """
            )
        )
        credentials = [_credential_config(row) for row in credential_rows if _is_exportable_credential(row)]
        exported_credential_ids = {credential["id"] for credential in credentials}
        schedules = [
            _schedule_config(row)
            for row in conn.execute(
                """
                SELECT id, name, enabled, interval_seconds, task_title, task_description,
                       priority, assigned_agent_id, credential_id, action, intent, next_run_at,
                       catch_up_policy
                FROM schedules
                ORDER BY id
                """
            )
            if not row["credential_id"] or row["credential_id"] in exported_credential_ids
        ]
    return {
        "version": CONFIG_VERSION,
        "agents": agents,
        "credentials": credentials,
        "schedules": schedules,
    }


def validate_declarative_config(store: HivemindStore, config: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _normalize_config(config)
    _validate_references(store, normalized["agents"], normalized["credentials"], normalized["schedules"])
    _validate_store_credential_rows(store, normalized["credentials"])
    return {
        "valid": True,
        "dry_run": True,
        "applied": False,
        "plan": _build_plan(store, normalized),
    }


def import_declarative_config(
    store: HivemindStore,
    config: Mapping[str, Any],
    *,
    actor_id: str,
    dry_run: bool,
) -> dict[str, Any]:
    normalized = _normalize_config(config)
    _validate_references(store, normalized["agents"], normalized["credentials"], normalized["schedules"])
    _validate_store_credential_rows(store, normalized["credentials"])
    plan = _build_plan(store, normalized)
    if dry_run:
        return {"valid": True, "dry_run": True, "applied": False, "plan": plan}

    now = iso()
    with store.connect() as conn:
        for agent in normalized["agents"]:
            _upsert_agent(conn, agent, now)
        for credential in normalized["credentials"]:
            _upsert_credential(conn, store, credential)
        for schedule in normalized["schedules"]:
            _upsert_schedule(conn, schedule, now)

    store.audit(
        "config.imported",
        actor_id,
        CONFIG_TARGET_ID,
        "allowed",
        "declarative config imported",
        {
            "agents": len(normalized["agents"]),
            "credentials": len(normalized["credentials"]),
            "schedules": len(normalized["schedules"]),
        },
    )
    return {"valid": True, "dry_run": False, "applied": True, "plan": plan}


def _agent_config(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "role": row["role"],
        "provider": row["provider"],
        "model": row["model"],
        "system_prompt": row["system_prompt"],
    }


def _credential_config(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "provider": row["provider"],
        "secret_ref": row["secret_ref"],
        "policy": _credential_policy_config(row),
        "metadata": _export_metadata(loads(row["metadata"], {})),
    }


def _is_exportable_credential(row: sqlite3.Row) -> bool:
    metadata = loads(row["metadata"], {})
    scheme, _, _ = str(row["secret_ref"]).partition("://")
    if scheme in {BROKER_SECRET_REF_SCHEME, OAUTH_REF_SCHEME}:
        return False
    kind = metadata.get("credential_kind")
    if kind is not None and str(kind).strip().lower() == "managed_secret":
        return False
    return True


def _credential_policy_config(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "allowed_agents": loads(row["allowed_agents"], []),
        "allowed_actions": loads(row["allowed_actions"], []),
        "approval_required_actions": loads(row["approval_required_actions"], []),
        "max_ttl_seconds": row["max_ttl_seconds"],
        "require_intent": bool(row["require_intent"]),
    }


def _schedule_config(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "interval_seconds": row["interval_seconds"],
        "catch_up_policy": row["catch_up_policy"],
        "next_run_at": row["next_run_at"],
        "task_template": {
            "title": row["task_title"],
            "description": row["task_description"],
            "priority": row["priority"],
            "assigned_agent_id": row["assigned_agent_id"],
            "credential_id": row["credential_id"],
            "action": row["action"],
            "intent": row["intent"],
        },
    }


def _normalize_config(config: Mapping[str, Any]) -> dict[str, Any]:
    root = _mapping(config, "config")
    _reject_unknown_keys(root, {"version", "agents", "credentials", "schedules"}, "config")
    version = root.get("version")
    if version != CONFIG_VERSION:
        raise DeclarativeConfigError(f"config.version must be {CONFIG_VERSION}")
    agents = _normalize_agents(_list(root.get("agents", []), "agents"))
    credentials = _normalize_credentials(_list(root.get("credentials", []), "credentials"))
    schedules = _normalize_schedules(_list(root.get("schedules", []), "schedules"))
    return {
        "agents": agents,
        "credentials": credentials,
        "schedules": schedules,
    }


def _normalize_agents(items: Sequence[Any]) -> list[dict[str, Any]]:
    agents = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        agent = _mapping(item, f"agents[{index}]")
        _reject_unknown_keys(
            agent,
            {"id", "name", "role", "provider", "model", "system_prompt"},
            f"agents[{index}]",
        )
        agent_id = _required_str(agent, "id", f"agents[{index}]")
        if agent_id in seen:
            raise DeclarativeConfigError(f"agents[{index}].id duplicates {agent_id}")
        seen.add(agent_id)
        agents.append(
            {
                "id": agent_id,
                "name": _required_str(agent, "name", f"agents[{index}]"),
                "role": _required_str(agent, "role", f"agents[{index}]"),
                "provider": _required_str(agent, "provider", f"agents[{index}]"),
                "model": _required_str(agent, "model", f"agents[{index}]"),
                "system_prompt": _optional_str(agent, "system_prompt") or "",
            }
        )
    return agents


def _normalize_credentials(items: Sequence[Any]) -> list[dict[str, Any]]:
    credentials = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        prefix = f"credentials[{index}]"
        credential = _mapping(item, prefix)
        _reject_unknown_keys(
            credential,
            {"id", "name", "provider", "secret_ref", "policy", "metadata"},
            prefix,
        )
        credential_id = _required_str(credential, "id", prefix)
        if credential_id in seen:
            raise DeclarativeConfigError(f"{prefix}.id duplicates {credential_id}")
        seen.add(credential_id)
        secret_ref = _required_str(credential, "secret_ref", prefix)
        try:
            validate_external_secret_ref(secret_ref)
        except ValueError as exc:
            raise DeclarativeConfigError(f"{prefix}.secret_ref {exc}") from exc
        scheme, _, _ = secret_ref.partition("://")
        if scheme == OAUTH_REF_SCHEME:
            raise DeclarativeConfigError(f"{prefix}.secret_ref {OAUTH_REF_ERROR}")
        policy = _mapping(credential.get("policy"), f"{prefix}.policy")
        _reject_unknown_keys(
            policy,
            {
                "allowed_agents",
                "allowed_actions",
                "approval_required_actions",
                "max_ttl_seconds",
                "require_intent",
            },
            f"{prefix}.policy",
        )
        metadata = _metadata(credential.get("metadata", {}), f"{prefix}.metadata")
        try:
            validate_external_credential_metadata(metadata)
        except ValueError as exc:
            raise DeclarativeConfigError(f"{prefix}.metadata {exc}") from exc
        credentials.append(
            {
                "id": credential_id,
                "name": _required_str(credential, "name", prefix),
                "provider": _required_str(credential, "provider", prefix),
                "secret_ref": secret_ref,
                "allowed_agents": _string_list(policy.get("allowed_agents", []), f"{prefix}.policy.allowed_agents"),
                "allowed_actions": _action_list(policy.get("allowed_actions", []), f"{prefix}.policy.allowed_actions"),
                "approval_required_actions": _action_list(
                    policy.get("approval_required_actions"),
                    f"{prefix}.policy.approval_required_actions",
                ),
                "max_ttl_seconds": _int(policy.get("max_ttl_seconds", 300), f"{prefix}.policy.max_ttl_seconds", 1, 3600),
                "require_intent": _bool(policy.get("require_intent", True), f"{prefix}.policy.require_intent"),
                "metadata": metadata,
            }
        )
    return credentials


def _normalize_schedules(items: Sequence[Any]) -> list[dict[str, Any]]:
    schedules = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        prefix = f"schedules[{index}]"
        schedule = _mapping(item, prefix)
        _reject_unknown_keys(
            schedule,
            {
                "id",
                "name",
                "enabled",
                "interval_seconds",
                "catch_up_policy",
                "next_run_at",
                "task_template",
            },
            prefix,
        )
        schedule_id = _required_str(schedule, "id", prefix)
        if schedule_id in seen:
            raise DeclarativeConfigError(f"{prefix}.id duplicates {schedule_id}")
        seen.add(schedule_id)
        interval = _int(schedule.get("interval_seconds"), f"{prefix}.interval_seconds", 60)
        next_run_at = _schedule_next_run_at(schedule, prefix, interval)
        task_template = _mapping(schedule.get("task_template"), f"{prefix}.task_template")
        _reject_unknown_keys(
            task_template,
            {
                "title",
                "description",
                "priority",
                "assigned_agent_id",
                "credential_id",
                "action",
                "intent",
            },
            f"{prefix}.task_template",
        )
        schedules.append(
            {
                "id": schedule_id,
                "name": _required_str(schedule, "name", prefix),
                "enabled": _bool(schedule.get("enabled", True), f"{prefix}.enabled"),
                "interval_seconds": interval,
                "catch_up_policy": _catch_up_policy(
                    schedule.get("catch_up_policy"),
                    f"{prefix}.catch_up_policy",
                ),
                "next_run_at": next_run_at,
                "task_title": _required_str(task_template, "title", f"{prefix}.task_template"),
                "task_description": _optional_str(task_template, "description") or "",
                "priority": _optional_str(task_template, "priority") or "normal",
                "assigned_agent_id": _optional_str(task_template, "assigned_agent_id"),
                "credential_id": _optional_str(task_template, "credential_id"),
                "action": _optional_str(task_template, "action") or "",
                "intent": _optional_str(task_template, "intent") or "",
            }
        )
    return schedules


def _validate_references(
    store: HivemindStore,
    agents: list[dict[str, Any]],
    credentials: list[dict[str, Any]],
    schedules: list[dict[str, Any]],
) -> None:
    with store.connect() as conn:
        agent_ids = _existing_ids(conn, "agents") | {agent["id"] for agent in agents}
        credential_by_id = _existing_credential_policies(conn)
    credential_by_id.update({credential["id"]: credential for credential in credentials})
    _validate_credential_references(credentials, agent_ids)
    _validate_schedule_references(schedules, agent_ids, credential_by_id)


def _validate_credential_references(
    credentials: list[dict[str, Any]],
    agent_ids: set[str],
) -> None:
    for index, credential in enumerate(credentials):
        for agent_id in credential["allowed_agents"]:
            if agent_id not in agent_ids:
                raise DeclarativeConfigError(
                    f"credentials[{index}].policy.allowed_agents references unknown agent: {agent_id}"
                )
        if not credential["allowed_actions"]:
            raise DeclarativeConfigError(f"credentials[{index}].policy.allowed_actions must not be empty")
        for action in credential["approval_required_actions"]:
            if action not in credential["allowed_actions"]:
                raise DeclarativeConfigError(
                    f"credentials[{index}].policy.approval_required_actions is outside allowed_actions: {action}"
                )


def _validate_schedule_references(
    schedules: list[dict[str, Any]],
    agent_ids: set[str],
    credential_by_id: Mapping[str, dict[str, Any]],
) -> None:
    for index, schedule in enumerate(schedules):
        agent_id = schedule["assigned_agent_id"]
        credential_id = schedule["credential_id"]
        if agent_id and agent_id not in agent_ids:
            raise DeclarativeConfigError(f"schedules[{index}].task_template.assigned_agent_id references unknown agent: {agent_id}")
        if credential_id and credential_id not in credential_by_id:
            raise DeclarativeConfigError(
                f"schedules[{index}].task_template.credential_id references unknown credential: {credential_id}"
            )
        if credential_id:
            _validate_schedule_credential_policy(index, schedule, credential_by_id[credential_id])


def _validate_schedule_credential_policy(
    index: int,
    schedule: Mapping[str, Any],
    credential: Mapping[str, Any],
) -> None:
    agent_id = schedule["assigned_agent_id"]
    action = schedule["action"].strip().lower()
    if not agent_id:
        raise DeclarativeConfigError(
            f"schedules[{index}].task_template.assigned_agent_id is required when credential_id is set"
        )
    if agent_id not in credential["allowed_agents"]:
        raise DeclarativeConfigError(
            f"schedules[{index}].task_template.assigned_agent_id is outside credential policy: {agent_id}"
        )
    if action not in credential["allowed_actions"]:
        raise DeclarativeConfigError(
            f"schedules[{index}].task_template.action is outside credential policy: {action}"
        )
    if credential["require_intent"] and len(schedule["intent"].strip()) < 12:
        raise DeclarativeConfigError(
            f"schedules[{index}].task_template.intent is too short for credential policy"
        )


def _validate_store_credential_rows(store: HivemindStore, credentials: list[dict[str, Any]]) -> None:
    for index, credential in enumerate(credentials):
        try:
            store._prepare_credential_row(dict(credential))
        except StoreError as exc:
            raise DeclarativeConfigError(f"credentials[{index}] {exc}") from exc


def _build_plan(store: HivemindStore, normalized: Mapping[str, list[dict[str, Any]]]) -> dict[str, dict[str, int]]:
    with store.connect() as conn:
        existing = {
            "agents": _existing_ids(conn, "agents"),
            "credentials": _existing_ids(conn, "credentials"),
            "schedules": _existing_ids(conn, "schedules"),
        }
    return {
        key: _plan_counts([item["id"] for item in normalized[key]], existing[key])
        for key in ("agents", "credentials", "schedules")
    }


def _upsert_agent(conn: sqlite3.Connection, agent: Mapping[str, Any], now: str) -> None:
    exists = _row_exists(conn, "agents", agent["id"])
    if exists:
        conn.execute(
            """
            UPDATE agents
            SET name = ?, role = ?, provider = ?, model = ?, system_prompt = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                agent["name"],
                agent["role"],
                agent["provider"],
                agent["model"],
                agent["system_prompt"],
                now,
                agent["id"],
            ),
        )
        return
    conn.execute(
        """
        INSERT INTO agents (id, name, role, provider, model, status, system_prompt, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            agent["id"],
            agent["name"],
            agent["role"],
            agent["provider"],
            agent["model"],
            "idle",
            agent["system_prompt"],
            now,
            now,
        ),
    )


def _upsert_credential(conn: sqlite3.Connection, store: HivemindStore, credential: Mapping[str, Any]) -> None:
    row = store._prepare_credential_row(dict(credential))
    exists = _row_exists(conn, "credentials", row["id"])
    if exists:
        conn.execute(
            """
            UPDATE credentials
            SET name = ?, provider = ?, secret_ref = ?, allowed_agents = ?, allowed_actions = ?,
                approval_required_actions = ?, max_ttl_seconds = ?, require_intent = ?,
                metadata = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                row["name"],
                row["provider"],
                row["secret_ref"],
                row["allowed_agents"],
                row["allowed_actions"],
                row["approval_required_actions"],
                row["max_ttl_seconds"],
                row["require_intent"],
                row["metadata"],
                row["updated_at"],
                row["id"],
            ),
        )
        return
    conn.execute(
        """
        INSERT INTO credentials
        (id, name, provider, secret_ref, allowed_agents, allowed_actions,
         approval_required_actions, max_ttl_seconds, require_intent, metadata,
         created_at, updated_at)
        VALUES (:id, :name, :provider, :secret_ref, :allowed_agents, :allowed_actions,
                :approval_required_actions, :max_ttl_seconds, :require_intent,
                :metadata, :created_at, :updated_at)
        """,
        row,
    )


def _upsert_schedule(conn: sqlite3.Connection, schedule: Mapping[str, Any], now: str) -> None:
    exists = _row_exists(conn, "schedules", schedule["id"])
    values = (
        schedule["name"],
        1 if schedule["enabled"] else 0,
        schedule["interval_seconds"],
        schedule["catch_up_policy"],
        schedule["task_title"],
        schedule["task_description"],
        schedule["priority"],
        schedule["assigned_agent_id"],
        schedule["credential_id"],
        schedule["action"],
        schedule["intent"],
        schedule["next_run_at"],
        now,
        schedule["id"],
    )
    if exists:
        conn.execute(
            """
            UPDATE schedules
            SET name = ?, enabled = ?, interval_seconds = ?, catch_up_policy = ?, task_title = ?,
                task_description = ?, priority = ?, assigned_agent_id = ?,
                credential_id = ?, action = ?, intent = ?, next_run_at = ?, updated_at = ?
            WHERE id = ?
            """,
            values,
        )
        return
    conn.execute(
        """
        INSERT INTO schedules
        (name, enabled, interval_seconds, catch_up_policy, task_title, task_description, priority,
         assigned_agent_id, credential_id, action, intent, next_run_at, updated_at,
         id, last_run_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
        """,
        (*values, now),
    )


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DeclarativeConfigError(f"{name} must be an object")
    return value


def _list(value: Any, name: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise DeclarativeConfigError(f"{name} must be a list")
    return value


def _required_str(values: Mapping[str, Any], key: str, prefix: str) -> str:
    value = _optional_str(values, key)
    if value is None:
        raise DeclarativeConfigError(f"{prefix}.{key} must be a non-empty string")
    return value


def _optional_str(values: Mapping[str, Any], key: str) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise DeclarativeConfigError(f"{key} must be a string")
    value = value.strip()
    return value or None


def _string_list(value: Any, name: str) -> list[str]:
    items = _list(value, name)
    normalized = []
    for index, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            raise DeclarativeConfigError(f"{name}[{index}] must be a non-empty string")
        normalized.append(item.strip())
    return sorted(set(normalized))


def _action_list(value: Any, name: str) -> list[str]:
    return sorted(set(item.lower() for item in _string_list(value, name)))


def _int(value: Any, name: str, minimum: int, maximum: int | None = None) -> int:
    if not isinstance(value, int):
        raise DeclarativeConfigError(f"{name} must be an integer")
    if maximum is None:
        if value < minimum:
            raise DeclarativeConfigError(f"{name} must be at least {minimum}")
    elif value < minimum or value > maximum:
        raise DeclarativeConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise DeclarativeConfigError(f"{name} must be true or false")
    return value


def _schedule_next_run_at(schedule: Mapping[str, Any], prefix: str, interval_seconds: int) -> str:
    name = f"{prefix}.next_run_at"
    value = _optional_str(schedule, "next_run_at") or iso(utcnow() + timedelta(seconds=interval_seconds))
    try:
        parsed = parse_dt(value)
    except ValueError as exc:
        raise DeclarativeConfigError(f"{name} must be a valid ISO timestamp") from exc
    if parsed is None:
        raise DeclarativeConfigError(f"{name} must be a valid ISO timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DeclarativeConfigError(f"{name} must include a timezone")
    return value


def _catch_up_policy(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeclarativeConfigError(f"{name} must be a non-empty string")
    normalized = value.strip().lower()
    if normalized not in SCHEDULE_CATCH_UP_POLICIES:
        allowed = ", ".join(SCHEDULE_CATCH_UP_POLICIES)
        raise DeclarativeConfigError(f"{name} must be one of: {allowed}")
    return normalized


def _metadata(value: Any, name: str) -> dict[str, Any]:
    metadata = dict(_mapping(value, name))
    _reject_secret_metadata_keys(metadata, name)
    try:
        dumps(metadata)
    except TypeError as exc:
        raise DeclarativeConfigError(f"{name} must be JSON serializable") from exc
    return metadata


def _reject_unknown_keys(values: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise DeclarativeConfigError(f"{name}.{unknown[0]} is not supported")


def _reject_secret_metadata_keys(value: Any, name: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_name = f"{name}.{key}"
            if _is_forbidden_metadata_key(key):
                raise DeclarativeConfigError(f"{child_name} cannot contain secret material")
            _reject_secret_metadata_keys(child, child_name)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_metadata_keys(child, f"{name}[{index}]")


def _export_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        if _is_forbidden_metadata_key(key):
            continue
        safe[key] = _export_metadata_value(value)
    return safe


def _export_metadata_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _export_metadata(value)
    if isinstance(value, list):
        return [_export_metadata_value(item) for item in value]
    return value


def _is_forbidden_metadata_key(key: object) -> bool:
    key_name = str(key).lower()
    compact_key = "".join(character for character in key_name if character.isalnum())
    if key_name in FORBIDDEN_METADATA_KEYS or compact_key in COMPACT_FORBIDDEN_METADATA_KEYS:
        return True
    return any(fragment in compact_key for fragment in FORBIDDEN_METADATA_FRAGMENTS)


def _existing_ids(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["id"] for row in conn.execute(EXISTING_ID_QUERIES[table])}


def _existing_credential_policies(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {
        row["id"]: {
            "id": row["id"],
            "allowed_agents": loads(row["allowed_agents"], []),
            "allowed_actions": loads(row["allowed_actions"], []),
            "require_intent": bool(row["require_intent"]),
        }
        for row in conn.execute(
            """
            SELECT id, allowed_agents, allowed_actions, require_intent
            FROM credentials
            """
        )
    }


def _row_exists(conn: sqlite3.Connection, table: str, row_id: str) -> bool:
    row = conn.execute(ROW_EXISTS_QUERIES[table], (row_id,)).fetchone()
    return row is not None


def _plan_counts(ids: Sequence[str], existing_ids: set[str]) -> dict[str, int]:
    return {
        "create": sum(1 for item_id in ids if item_id not in existing_ids),
        "update": sum(1 for item_id in ids if item_id in existing_ids),
    }
