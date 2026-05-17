from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from hivemind.__main__ import main
from hivemind.store import BACKUP_FORMAT, BACKUP_FORMAT_VERSION, BACKUP_TABLE_QUERIES, HivemindStore, StoreValidationError

TEST_PASSWORD = "operator-not-secret"  # nosec B105
TABLE_COUNT_QUERIES = {
    "sessions": "SELECT COUNT(*) FROM sessions",
    "leases": "SELECT COUNT(*) FROM leases",
    "oauth_states": "SELECT COUNT(*) FROM oauth_states",
    "oauth_connections": "SELECT COUNT(*) FROM oauth_connections",
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
    source.record_heartbeat(task["id"], agent["id"], "backup rehearsal still running")
    source.create_schedule(
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


def test_cli_backup_and_restore_commands_use_hivemind_db_path(tmp_path: Path, monkeypatch) -> None:
    source_db = tmp_path / "cli-source.db"
    source = HivemindStore(source_db)
    source.setup_admin("admin", TEST_PASSWORD)

    backup_path = tmp_path / "backup.json"
    monkeypatch.setenv("HIVEMIND_DB_PATH", str(source_db))
    require_equal(main(["backup", str(backup_path)]), 0, "backup command should exit cleanly")

    backup_bundle = json.loads(backup_path.read_text())
    require_equal(backup_bundle["format"], BACKUP_FORMAT, "cli backup should emit the logical backup format")

    target_db = tmp_path / "cli-target.db"
    target = HivemindStore(target_db)
    target.setup_admin("staleadmin", TEST_PASSWORD)
    monkeypatch.setenv("HIVEMIND_DB_PATH", str(target_db))
    require_equal(main(["restore", str(backup_path)]), 0, "restore command should exit cleanly")

    require_equal(table_rows(target_db, "users"), backup_bundle["tables"]["users"], "cli restore should replace users")
