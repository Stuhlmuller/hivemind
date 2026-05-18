from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3

from hivemind.__main__ import main
from hivemind.oauth import SecretBox
from hivemind.store import BACKUP_FORMAT, BACKUP_FORMAT_VERSION, BACKUP_TABLE_QUERIES, HivemindStore, StoreValidationError

TEST_PASSWORD = "operator-not-secret"  # nosec B105
MANAGED_SECRET_VALUE = "example"  # nosec B105
STALE_MANAGED_SECRET_VALUE = "stale-example"  # nosec B105
TABLE_COUNT_QUERIES = {
    "sessions": "SELECT COUNT(*) FROM sessions",
    "leases": "SELECT COUNT(*) FROM leases",
    "oauth_states": "SELECT COUNT(*) FROM oauth_states",
    "oauth_connections": "SELECT COUNT(*) FROM oauth_connections",
    "broker_secrets": "SELECT COUNT(*) FROM broker_secrets",
}


def require_equal(actual: object, expected: object, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def require_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def table_rows(db_path: Path, table: str) -> list[dict[str, object]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(BACKUP_TABLE_QUERIES[table])]
    finally:
        conn.close()


def table_count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(TABLE_COUNT_QUERIES[table]).fetchone()[0]
    finally:
        conn.close()


def test_backup_bundle_round_trip_restores_durable_state_and_clears_ephemeral_state(tmp_path: Path) -> None:
    source_db = tmp_path / "source.db"
    source = HivemindStore(source_db)
    source_admin = source.setup_admin("admin", TEST_PASSWORD)
    source.login("admin", TEST_PASSWORD)

    agent = source.create_agent(
        {
            "name": "Restorer",
            "role": "Prepare logical backups.",
            "provider": "local",
            "model": "deterministic-policy",
            "system_prompt": "Be concise.",
        }
    )
    credential = source.create_credential(
        {
            "name": "Vault reference",
            "provider": "github",
            "secret_ref": "vault://ops/github",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["read_repo"],
            "max_ttl_seconds": 180,
            "require_intent": True,
            "agent_lease_limit": 3,
            "credential_lease_limit": 7,
            "credential_action_limit": 11,
            "rate_limit_window_seconds": 900,
            "provider_token_budget": 5000,
            "provider_cost_budget_cents": 250,
            "metadata": {"purpose": "backup coverage"},
        }
    )
    oauth_credential = source.create_credential(
        {
            "name": "OAuth credential",
            "provider": "codex",
            "secret_ref": "oauth://codex/cred_example",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["exchange_oauth_code"],
            "max_ttl_seconds": 180,
            "require_intent": True,
            "metadata": {"auth_type": "oauth"},
        }
    )
    managed_credential = source.create_managed_credential(
        {
            "name": "Broker-managed secret",
            "provider": "github",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["read_repo"],
            "max_ttl_seconds": 180,
            "require_intent": True,
        },
        secret_value=MANAGED_SECRET_VALUE,
        secret_box=SecretBox("source-backup-secret-key"),
    )
    task = source.create_task(
        {
            "title": "Backup repo state",
            "description": "Capture durable state for operators.",
            "priority": "normal",
            "assigned_agent_id": agent["id"],
            "credential_id": credential["id"],
            "action": "read_repo",
            "intent": "Read repository state before writing a logical backup bundle.",
            "heartbeat_seconds": 60,
        }
    )
    oauth_task = source.create_task(
        {
            "title": "Reconnect OAuth credential",
            "description": "Keep the task while dropping the non-restorable OAuth capability link.",
            "priority": "normal",
            "assigned_agent_id": agent["id"],
            "credential_id": oauth_credential["id"],
            "action": "exchange_oauth_code",
            "intent": "Reconnect the brokered OAuth credential after logical restore completes.",
            "heartbeat_seconds": None,
        }
    )
    managed_task = source.create_task(
        {
            "title": "Reconnect managed credential",
            "description": "Keep the task while dropping the non-restorable managed secret link.",
            "priority": "normal",
            "assigned_agent_id": agent["id"],
            "credential_id": managed_credential["id"],
            "action": "read_repo",
            "intent": "Reconnect the broker-managed credential after logical restore completes.",
            "heartbeat_seconds": None,
        }
    )
    source.record_heartbeat(task["id"], agent["id"], "backup rehearsal still running")
    schedule = source.create_schedule(
        {
            "name": "Nightly backup rehearsal",
            "enabled": True,
            "interval_seconds": 3600,
            "task_title": "Verify backup plan",
            "task_description": "Check durable state coverage.",
            "priority": "normal",
            "assigned_agent_id": agent["id"],
            "credential_id": credential["id"],
            "action": "read_repo",
            "intent": "Inspect repository state for a safe backup rehearsal.",
            "next_run_at": "2030-01-01T00:00:00+00:00",
        }
    )
    oauth_schedule = source.create_schedule(
        {
            "name": "OAuth reconnect reminder",
            "enabled": True,
            "interval_seconds": 7200,
            "task_title": "Reconnect OAuth credential",
            "task_description": "Recreate the brokered OAuth connection after restore.",
            "priority": "normal",
            "assigned_agent_id": agent["id"],
            "credential_id": oauth_credential["id"],
            "action": "exchange_oauth_code",
            "intent": "Reconnect the brokered OAuth credential after logical restore completes.",
            "next_run_at": "2030-01-01T01:00:00+00:00",
        }
    )
    managed_schedule = source.create_schedule(
        {
            "name": "Managed secret reconnect reminder",
            "enabled": True,
            "interval_seconds": 7200,
            "task_title": "Reconnect managed credential",
            "task_description": "Recreate the broker-managed secret after restore.",
            "priority": "normal",
            "assigned_agent_id": agent["id"],
            "credential_id": managed_credential["id"],
            "action": "read_repo",
            "intent": "Reconnect the broker-managed credential after logical restore completes.",
            "next_run_at": "2030-01-01T02:00:00+00:00",
        }
    )
    source.audit(
        "backup.bundle.prepared",
        source_admin["id"],
        task["id"],
        "allowed",
        "prepared logical backup bundle",
        {"scope": "test"},
    )

    bundle = source.export_backup_bundle()

    require_equal(bundle["format"], BACKUP_FORMAT, "bundle format should match the logical backup identifier")
    require_equal(
        bundle["format_version"],
        BACKUP_FORMAT_VERSION,
        "bundle format version should match the current restore format",
    )
    exported_credential_ids = {row["id"] for row in bundle["tables"]["credentials"]}
    require_true(credential["id"] in exported_credential_ids, "vault credentials should be exported")
    require_true(oauth_credential["id"] not in exported_credential_ids, "oauth credentials should be excluded")
    require_true(
        managed_credential["id"] not in exported_credential_ids,
        "broker-managed credentials should be excluded",
    )
    exported_credential = next(row for row in bundle["tables"]["credentials"] if row["id"] == credential["id"])
    require_equal(exported_credential["agent_lease_limit"], 3, "agent lease limits should be exported")
    require_equal(exported_credential["credential_lease_limit"], 7, "credential lease limits should be exported")
    require_equal(exported_credential["credential_action_limit"], 11, "credential action limits should be exported")
    require_equal(exported_credential["rate_limit_window_seconds"], 900, "rate windows should be exported")
    require_equal(exported_credential["provider_token_budget"], 5000, "provider token budgets should be exported")
    require_equal(
        exported_credential["provider_cost_budget_cents"],
        250,
        "provider cost budgets should be exported",
    )
    exported_tasks = {row["id"]: row for row in bundle["tables"]["tasks"]}
    exported_schedules = {row["id"]: row for row in bundle["tables"]["schedules"]}
    require_equal(
        exported_tasks[task["id"]]["credential_id"],
        credential["id"],
        "restorable task credential refs should be preserved",
    )
    require_equal(
        exported_tasks[oauth_task["id"]]["credential_id"],
        None,
        "tasks should drop refs to excluded oauth credentials",
    )
    require_equal(
        exported_tasks[managed_task["id"]]["credential_id"],
        None,
        "tasks should drop refs to excluded broker-managed credentials",
    )
    require_equal(
        exported_schedules[schedule["id"]]["credential_id"],
        credential["id"],
        "restorable schedule credential refs should be preserved",
    )
    require_equal(
        exported_schedules[oauth_schedule["id"]]["credential_id"],
        None,
        "schedules should drop refs to excluded oauth credentials",
    )
    require_equal(
        exported_schedules[managed_schedule["id"]]["credential_id"],
        None,
        "schedules should drop refs to excluded broker-managed credentials",
    )

    target_db = tmp_path / "target.db"
    target = HivemindStore(target_db)
    target_admin = target.setup_admin("staleadmin", TEST_PASSWORD)
    target.login("staleadmin", TEST_PASSWORD)
    stale_agent = target.list_agents()[0]
    target.request_lease(
        credential_id="cred_demo_github",
        agent_id=stale_agent["id"],
        action="open_issue",
        intent="Create a bounded pre-restore lease for restore cleanup coverage.",
        ttl_seconds=30,
    )
    target.create_managed_credential(
        {
            "name": "Stale managed secret",
            "provider": "github",
            "allowed_agents": [stale_agent["id"]],
            "allowed_actions": ["read_repo"],
            "max_ttl_seconds": 120,
            "require_intent": True,
        },
        secret_value=STALE_MANAGED_SECRET_VALUE,
        secret_box=SecretBox("target-backup-secret-key"),
    )
    target.create_oauth_state(
        user_id=target_admin["id"],
        provider="codex",
        pkce_verifier="pkce-verifier",
        credential_payload={"name": "stale oauth", "allowed_actions": ["exchange_oauth_code"]},
    )

    restore_summary = target.restore_backup_bundle(bundle)

    require_equal(restore_summary, bundle["summary"], "restore should report the exported table counts")
    for table, rows in bundle["tables"].items():
        require_equal(table_rows(target_db, table), rows, f"{table} should round-trip through restore")

    require_equal(table_count(target_db, "sessions"), 0, "restore should clear active sessions")
    require_equal(table_count(target_db, "leases"), 0, "restore should clear active leases")
    require_equal(table_count(target_db, "oauth_states"), 0, "restore should clear pending oauth states")
    require_equal(
        table_count(target_db, "oauth_connections"),
        0,
        "restore should not recreate broker-owned oauth token material",
    )
    require_equal(
        table_count(target_db, "broker_secrets"),
        0,
        "restore should not recreate broker-managed secret material",
    )


def test_backup_round_trip_preserves_custom_tool_actions(tmp_path: Path) -> None:
    source_db = tmp_path / "source.db"
    source = HivemindStore(source_db)
    source.setup_admin("admin", TEST_PASSWORD)
    agent = source.create_agent(
        {
            "name": "Registry Restorer",
            "role": "Verify tool action registry backups.",
        }
    )
    credential = source.create_credential(
        {
            "name": "Read-only repository token",
            "provider": "github",
            "secret_ref": "env://HIVEMIND_BACKUP_TEST_TOKEN",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["read_repo"],
            "max_ttl_seconds": 180,
            "require_intent": True,
        }
    )
    tool_action = source.create_tool_action(
        {
            "name": "repo_status",
            "description": "Read repository status through the read_repo credential scope.",
            "required_credential_action": "read_repo",
            "risk_level": "low",
            "input_schema": {
                "type": "object",
                "properties": {"repo": {"type": "string"}},
                "required": ["repo"],
                "additionalProperties": False,
            },
        }
    )
    task = source.create_task(
        {
            "title": "Restore tool action task",
            "description": "Exercise a restored custom tool action.",
            "priority": "normal",
            "assigned_agent_id": agent["id"],
            "credential_id": credential["id"],
            "action": tool_action["name"],
            "intent": "Read repository status after logical restore.",
        }
    )
    schedule = source.create_schedule(
        {
            "name": "Restore tool action schedule",
            "enabled": True,
            "interval_seconds": 3600,
            "task_title": "Restore scheduled tool action task",
            "task_description": "Exercise a restored scheduled custom tool action.",
            "priority": "normal",
            "assigned_agent_id": agent["id"],
            "credential_id": credential["id"],
            "action": tool_action["name"],
            "intent": "Read scheduled repository status after logical restore.",
            "next_run_at": "2030-01-01T00:00:00+00:00",
        }
    )

    bundle = source.export_backup_bundle()
    target_db = tmp_path / "target.db"
    target = HivemindStore(target_db)
    target.restore_backup_bundle(bundle)
    restored_action = target.get_tool_action(tool_action["name"])
    token, lease = target.request_lease(
        credential["id"],
        agent["id"],
        tool_action["name"],
        "Read repository status after restoring the tool action registry.",
        30,
    )
    result = target.perform_credential_action(token or "", tool_action["name"], {"repo": "hivemind"})

    require_true(
        any(row["name"] == tool_action["name"] for row in bundle["tables"]["tool_actions"]),
        "custom tool actions should be exported",
    )
    require_equal(
        table_rows(target_db, "tool_actions"),
        bundle["tables"]["tool_actions"],
        "tool actions should round-trip through restore",
    )
    require_equal(
        restored_action["required_credential_action"],
        "read_repo",
        "restored tool action should preserve its mapped credential action",
    )
    require_equal(table_rows(target_db, "tasks")[0]["action"], task["action"], "restored tasks should keep custom tool actions")
    require_equal(
        table_rows(target_db, "schedules")[0]["action"],
        schedule["action"],
        "restored schedules should keep custom tool actions",
    )
    require_equal(lease["action"], tool_action["name"], "restored custom tool actions should issue exact scoped leases")
    require_equal(result["credential_action"], "read_repo", "restored custom tool actions should execute via their mapped scope")


def test_restore_rejects_incompatible_backup_version(tmp_path: Path) -> None:
    source = HivemindStore(tmp_path / "source.db")
    source.setup_admin("admin", TEST_PASSWORD)
    bundle = source.export_backup_bundle()
    bundle["format_version"] = BACKUP_FORMAT_VERSION + 1

    target = HivemindStore(tmp_path / "target.db")

    try:
        target.restore_backup_bundle(bundle)
    except StoreValidationError as exc:
        require_true(
            "unsupported backup format version" in str(exc),
            "restore should explain that the backup format version is unsupported",
        )
    else:
        raise AssertionError("restore should reject a mismatched backup format version")

    require_true(target.is_setup_complete() is False, "failed restores should not create a setup state")


def test_restore_accepts_v1_backup_without_hive_fields(tmp_path: Path) -> None:
    source = HivemindStore(tmp_path / "source.db")
    source.setup_admin("admin", TEST_PASSWORD)
    agent = source.list_agents()[0]
    task = source.create_task(
        {
            "title": "Legacy restore task",
            "assigned_agent_id": agent["id"],
            "heartbeat_seconds": None,
        }
    )
    schedule = source.create_schedule(
        {
            "name": "Legacy restore schedule",
            "interval_seconds": 3600,
            "task_title": "Legacy scheduled task",
            "assigned_agent_id": agent["id"],
            "next_run_at": "2030-01-01T00:00:00+00:00",
        }
    )
    legacy_bundle = json.loads(json.dumps(source.export_backup_bundle()))
    legacy_bundle["format_version"] = 1
    legacy_bundle["tables"].pop("hives", None)
    legacy_bundle["summary"].pop("hives", None)
    for row in legacy_bundle["tables"]["agents"]:
        row.pop("hive_id", None)
        row.pop("can_spawn_subagents", None)
        row.pop("max_subagents", None)
        row.pop("issue_creation_enabled", None)
        row.pop("issue_kind", None)
        row.pop("issue_rate_limit_per_hour", None)
        row.pop("issue_labels", None)
    for row in legacy_bundle["tables"]["tasks"]:
        row.pop("hive_id", None)
    for row in legacy_bundle["tables"]["schedules"]:
        row.pop("hive_id", None)

    target_db = tmp_path / "target.db"
    target = HivemindStore(target_db)
    restore_summary = target.restore_backup_bundle(legacy_bundle)

    require_equal(restore_summary["hives"], 0, "legacy backup restore should create no synthetic hives")
    restored_agents = table_rows(target_db, "agents")
    restored_tasks = table_rows(target_db, "tasks")
    restored_schedules = table_rows(target_db, "schedules")
    require_true(all(row["hive_id"] is None for row in restored_agents), "legacy agents should restore without hives")
    require_true(
        all(row["issue_creation_enabled"] == 0 for row in restored_agents),
        "legacy agents should restore with issue creation disabled",
    )
    restored_task = next(row for row in restored_tasks if row["id"] == task["id"])
    restored_schedule = next(row for row in restored_schedules if row["id"] == schedule["id"])
    require_equal(restored_task["hive_id"], None, "legacy tasks should restore without hive assignment")
    require_equal(restored_schedule["hive_id"], None, "legacy schedules should restore without hive assignment")


def test_restore_applies_defaults_for_legacy_credential_rate_limit_columns(tmp_path: Path) -> None:
    source = HivemindStore(tmp_path / "source.db")
    source.setup_admin("admin", TEST_PASSWORD)
    bundle = source.export_backup_bundle()
    credential = bundle["tables"]["credentials"][0]
    for field_name in (
        "agent_lease_limit",
        "credential_lease_limit",
        "credential_action_limit",
        "rate_limit_window_seconds",
        "provider_token_budget",
        "provider_cost_budget_cents",
    ):
        credential.pop(field_name)

    target = HivemindStore(tmp_path / "target.db")
    target.restore_backup_bundle(bundle)
    restored = table_rows(tmp_path / "target.db", "credentials")[0]

    require_equal(restored["agent_lease_limit"], None, "legacy restores should default agent lease limits")
    require_equal(
        restored["credential_lease_limit"],
        None,
        "legacy restores should default credential lease limits",
    )
    require_equal(
        restored["credential_action_limit"],
        None,
        "legacy restores should default credential action limits",
    )
    require_equal(restored["rate_limit_window_seconds"], 60, "legacy restores should default rate windows")
    require_equal(restored["provider_token_budget"], None, "legacy restores should default token budgets")
    require_equal(restored["provider_cost_budget_cents"], None, "legacy restores should default cost budgets")


def test_restore_rejects_unsafe_tool_action_identifiers(tmp_path: Path) -> None:
    source = HivemindStore(tmp_path / "source.db")
    source.setup_admin("admin", TEST_PASSWORD)
    bundle = source.export_backup_bundle()

    for column in ("name", "required_credential_action"):
        unsafe_bundle = json.loads(json.dumps(bundle))
        unsafe_bundle["tables"]["tool_actions"][0][column] = "token-unsafe"
        target = HivemindStore(tmp_path / f"target-{column}.db")

        try:
            target.restore_backup_bundle(unsafe_bundle)
        except StoreValidationError as exc:
            require_true(
                "actions must use lowercase snake_case names" in str(exc),
                f"restore should reject unsafe tool action {column}",
            )
        else:
            raise AssertionError(f"restore should reject unsafe tool action {column}")

        require_true(target.is_setup_complete() is False, "failed restores should not create a setup state")


def test_restore_normalizes_schedule_timestamp_offsets(tmp_path: Path) -> None:
    source = HivemindStore(tmp_path / "source.db")
    source.setup_admin("admin", TEST_PASSWORD)
    source.create_schedule(
        {
            "name": "Offset restore schedule",
            "interval_seconds": 60,
            "task_title": "Restored offset task",
            "next_run_at": "2030-01-01T00:00:00+00:00",
        }
    )
    bundle = source.export_backup_bundle()
    schedule = bundle["tables"]["schedules"][0]
    offset_zone = timezone(timedelta(hours=14))
    next_run_at = datetime(2030, 1, 1, 12, 0, tzinfo=offset_zone)
    last_run_at = datetime(2030, 1, 1, 11, 30, tzinfo=offset_zone)
    schedule["next_run_at"] = next_run_at.isoformat()
    schedule["last_run_at"] = last_run_at.isoformat()

    target = HivemindStore(tmp_path / "target.db")
    target.restore_backup_bundle(bundle)
    restored_schedule = table_rows(tmp_path / "target.db", "schedules")[0]

    require_equal(
        restored_schedule["next_run_at"],
        next_run_at.astimezone(timezone.utc).isoformat(),
        "restore should normalize schedule next_run_at to UTC",
    )
    require_equal(
        restored_schedule["last_run_at"],
        last_run_at.astimezone(timezone.utc).isoformat(),
        "restore should normalize schedule last_run_at to UTC",
    )


def test_restore_rejects_malformed_schedule_timestamp(tmp_path: Path) -> None:
    source = HivemindStore(tmp_path / "source.db")
    source.setup_admin("admin", TEST_PASSWORD)
    source.create_schedule(
        {
            "name": "Malformed restore schedule",
            "interval_seconds": 60,
            "task_title": "Malformed restore task",
            "next_run_at": "2030-01-01T00:00:00+00:00",
        }
    )
    bundle = source.export_backup_bundle()
    bundle["tables"]["schedules"][0]["next_run_at"] = "not-a-date"

    target = HivemindStore(tmp_path / "target.db")

    try:
        target.restore_backup_bundle(bundle)
    except StoreValidationError as exc:
        require_true(
            "schedule next_run_at must be a valid ISO datetime" in str(exc),
            "restore should explain malformed schedule timestamps without parser internals",
        )
    else:
        raise AssertionError("restore should reject malformed schedule timestamps")

    require_true(target.is_setup_complete() is False, "failed restores should not create a setup state")


def test_cli_backup_and_restore_commands_use_hivemind_db_path(tmp_path: Path, monkeypatch) -> None:
    source_db = tmp_path / "cli-source.db"
    source = HivemindStore(source_db)
    source.setup_admin("admin", TEST_PASSWORD)

    backup_path = tmp_path / "backup.json"
    monkeypatch.setenv("HIVEMIND_DB_PATH", str(source_db))
    old_umask = os.umask(0)
    try:
        require_equal(main(["backup", str(backup_path)]), 0, "backup command should exit cleanly")
    finally:
        os.umask(old_umask)

    backup_bundle = json.loads(backup_path.read_text())
    require_equal(backup_bundle["format"], BACKUP_FORMAT, "cli backup should emit the logical backup format")
    require_equal(
        backup_path.stat().st_mode & 0o777,
        0o600,
        "cli backup file should be readable only by the operator account",
    )

    target_db = tmp_path / "cli-target.db"
    target = HivemindStore(target_db)
    target.setup_admin("staleadmin", TEST_PASSWORD)
    monkeypatch.setenv("HIVEMIND_DB_PATH", str(target_db))
    require_equal(main(["restore", str(backup_path)]), 0, "restore command should exit cleanly")

    require_equal(table_rows(target_db, "users"), backup_bundle["tables"]["users"], "cli restore should replace users")


def test_cli_backup_rejects_missing_hivemind_db_path_without_creating_database(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    missing_db = tmp_path / "missing.db"
    backup_path = tmp_path / "backup.json"
    monkeypatch.setenv("HIVEMIND_DB_PATH", str(missing_db))

    require_equal(main(["backup", str(backup_path)]), 1, "backup command should reject missing source DB")

    captured = capsys.readouterr()
    require_true("configured database does not exist" in captured.err, "backup should explain the missing DB path")
    require_true(missing_db.exists() is False, "backup should not create a fresh source database")
    require_true(backup_path.exists() is False, "backup should not write an output bundle after source validation fails")


def test_cli_restore_reads_input_before_opening_destination_database(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    target_db = tmp_path / "target.db"
    malformed_backup = tmp_path / "malformed-backup.json"
    malformed_backup.write_text("{", encoding="utf-8")
    monkeypatch.setenv("HIVEMIND_DB_PATH", str(target_db))

    require_equal(main(["restore", str(malformed_backup)]), 1, "restore command should reject malformed input")

    captured = capsys.readouterr()
    require_true("Traceback" not in captured.err, "restore should not emit a raw traceback")
    require_true(target_db.exists() is False, "restore should not create the destination DB before reading input")


def test_backup_exports_declared_user_columns_from_upgraded_databases(tmp_path: Path) -> None:
    source = HivemindStore(tmp_path / "source.db")
    source.setup_admin("admin", TEST_PASSWORD)
    with sqlite3.connect(tmp_path / "source.db") as conn:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
        conn.execute("UPDATE users SET email = 'legacy@example.invalid'")

    bundle = source.export_backup_bundle()

    require_equal(
        set(bundle["tables"]["users"][0]),
        {"id", "username", "password_hash", "role", "created_at"},
        "backup users should include only restorable columns",
    )
    target = HivemindStore(tmp_path / "target.db")
    require_equal(
        target.restore_backup_bundle(bundle),
        bundle["summary"],
        "backup from upgraded user schema should restore cleanly",
    )


def test_cli_backup_reports_sqlite_failures(monkeypatch, capsys) -> None:
    def fail_from_env(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("hivemind.__main__.HivemindStore.from_env", fail_from_env)

    require_equal(main(["backup", "-"]), 1, "backup command should report sqlite failures cleanly")

    captured = capsys.readouterr()
    require_true("database is locked" in captured.err, "backup should print the sqlite failure")
    require_true("Traceback" not in captured.err, "backup should not emit a raw traceback")
