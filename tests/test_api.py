from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import secrets
import sqlite3
from threading import Barrier

from fastapi.testclient import TestClient

from hivemind.api import create_app
from hivemind.store import HivemindStore, StoreError, hash_password

TEST_PASSWORD = "operator-not-secret"


def client_for(tmp_path: Path) -> TestClient:
    store = HivemindStore(tmp_path / "hivemind.db")
    return TestClient(create_app(store, start_scheduler=False))


def require_equal(actual: object, expected: object, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def require_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def setup(client: TestClient) -> None:
    response = client.post(
        "/auth/setup",
        json={"username": "admin", "password": TEST_PASSWORD},
    )
    assert response.status_code == 201


def test_concurrent_setup_only_creates_one_bootstrap_admin(tmp_path: Path) -> None:
    db_path = tmp_path / "bootstrap-race.db"
    stores = [HivemindStore(db_path), HivemindStore(db_path)]
    start = Barrier(3)

    def create_admin(store: HivemindStore, username: str) -> tuple[str, str]:
        start.wait()
        try:
            user = store.setup_admin(username, TEST_PASSWORD)
            return ("ok", user["username"])
        except StoreError as exc:
            return ("error", str(exc))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(create_admin, stores[0], "admin"),
            executor.submit(create_admin, stores[1], "operator"),
        ]
        start.wait()
        results = [future.result() for future in futures]

    assert sum(1 for status, _ in results if status == "ok") == 1
    assert [detail for status, detail in results if status == "error"] == ["setup is already complete"]

    conn = sqlite3.connect(db_path)
    try:
        admin_count = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
        usernames = [row[0] for row in conn.execute("SELECT username FROM users")]
    finally:
        conn.close()

    assert admin_count == 1
    assert len(usernames) == 1


def test_frontend_is_served(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert "Hivemind" in response.text
    assert "/static/app.js" in response.text
    for required in ['name="username"', 'name="password"', 'autocomplete="new-password"']:
        if required not in response.text:
            raise AssertionError(f"missing expected auth form markup: {required}")
    username_input_start = response.text.index('name="username"')
    username_input_end = response.text.index("/>", username_input_start)
    password_input_start = response.text.index('name="password"')
    password_input_end = response.text.index("/>", password_input_start)
    username_markup = response.text[username_input_start:username_input_end]
    password_markup = response.text[password_input_start:password_input_end]
    if 'value="' in username_markup:
        raise AssertionError("username input should not ship with a preset value")
    if 'value="' in password_markup:
        raise AssertionError("password input should not ship with a preset value")


def test_credentials_frontend_route_is_served(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    response = client.get("/control/credentials")

    assert response.status_code == 200
    assert 'data-page-link="credentials"' in response.text
    assert "credential broker" in response.text


def test_auth_surface_uses_username_and_first_user_becomes_admin(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    frontend = client.get("/")
    setup_response = client.post(
        "/auth/setup",
        json={"username": "OperatorAdmin", "password": TEST_PASSWORD},
    )
    logout_response = client.post("/auth/logout")
    login_response = client.post(
        "/auth/login",
        json={"username": "OPERATORADMIN", "password": TEST_PASSWORD},
    )
    me_response = client.get("/me")

    require_equal(frontend.status_code, 200, "frontend should render")
    require_true('name="username"' in frontend.text, "frontend should render a username field")
    require_true('name="email"' not in frontend.text, "frontend should not render an email field")
    require_true("auth: username/password" in frontend.text, "frontend should describe username/password auth")

    require_equal(setup_response.status_code, 201, "setup should create the first admin")
    require_equal(setup_response.json()["user"]["username"], "operatoradmin", "setup should normalize the username")
    require_equal(setup_response.json()["user"]["role"], "admin", "first user should become admin")
    require_true("email" not in setup_response.json()["user"], "setup response should not expose an email field")

    require_equal(logout_response.status_code, 200, "logout should succeed after setup")

    require_equal(login_response.status_code, 200, "login should accept username and password")
    require_equal(login_response.json()["user"]["username"], "operatoradmin", "login should return the username")
    require_equal(login_response.json()["user"]["role"], "admin", "login should preserve the admin role")
    require_true("email" not in login_response.json()["user"], "login response should not expose an email field")

    require_equal(me_response.status_code, 200, "me should return the current session user")
    require_equal(me_response.json()["username"], "operatoradmin", "me should expose the username")
    require_equal(me_response.json()["role"], "admin", "me should expose the role")
    require_true("email" not in me_response.json(), "me should not expose an email field")


def test_persisted_sessions_store_only_token_hashes(tmp_path: Path) -> None:
    db_path = tmp_path / "hashed-sessions.db"
    store = HivemindStore(db_path)

    store.setup_admin("admin", TEST_PASSWORD)
    token, _ = store.login("admin", TEST_PASSWORD)

    conn = sqlite3.connect(db_path)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(sessions)")]
        session_hashes = [row[0] for row in conn.execute("SELECT token_hash FROM sessions")]
    finally:
        conn.close()

    require_true("token_hash" in columns, "sessions should store a token_hash column")
    require_true("token" not in columns, "sessions should not keep a plaintext token column")
    require_equal(session_hashes, [store.hash_token(token)], "sessions should persist only the hashed token")
    require_true(token not in session_hashes, "raw session tokens should not be stored")


def test_config_requires_login_and_redacts_reviewer_credential_ref_after_setup(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_INTENT_REVIEWER_CREDENTIAL_REF", "env://HIVEMIND_DEMO_GITHUB_TOKEN")
    client = client_for(tmp_path)

    assert client.get("/config").status_code == 401
    setup(client)
    response = client.get("/config")
    reviewer = response.json()["intent_reviewer"]
    credential = client.get("/credentials").json()[0]

    assert response.status_code == 200
    assert reviewer["provider"] == "local"
    assert reviewer["credential_ref_preview"] == "env://HIV..."
    assert reviewer["credential_ref_preview"] == credential["secret_ref_preview"]
    assert "credential_ref" not in reviewer
    assert "HIVEMIND_DEMO_GITHUB_TOKEN" not in response.text


def test_authenticated_jit_lease_flow_redacts_secret_ref(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)
    agent = client.get("/agents").json()[0]
    credential = client.get("/credentials").json()[0]

    assert "HIVEMIND_DEMO_GITHUB_TOKEN" not in credential["secret_ref_preview"]
    lease_response = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": agent["id"],
            "action": "read_repo",
            "intent": "Read repository metadata for safe task triage.",
            "ttl_seconds": 30,
        },
    )
    assert lease_response.status_code == 201
    lease = lease_response.json()
    assert lease["lease_token"].startswith("hvl_")

    action_response = client.post(
        "/credential-actions",
        json={"lease_token": lease["lease_token"], "action": "read_repo", "payload": {"repo": "hivemind"}},
    )
    assert action_response.status_code == 200
    assert action_response.json()["ok"] is True


def test_operational_endpoints_return_401_before_auth(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    protected_requests = [
        ("GET", "/me", None),
        ("GET", "/config", None),
        ("GET", "/agents", None),
        (
            "POST",
            "/agents",
            {
                "name": "forager",
                "role": "Find the next useful task.",
                "provider": "local",
                "model": "deterministic-policy",
                "system_prompt": "Respond briefly.",
            },
        ),
        ("GET", "/credentials", None),
        (
            "POST",
            "/credentials",
            {
                "name": "Repo reader",
                "provider": "github",
                "secret_ref": "env://GITHUB_TOKEN",
                "allowed_agents": [],
                "allowed_actions": ["read_repo"],
                "max_ttl_seconds": 60,
                "require_intent": True,
                "metadata": {},
            },
        ),
        ("GET", "/credential-leases", None),
        (
            "POST",
            "/credential-leases",
            {
                "credential_id": "cred_demo",
                "agent_id": "agent_demo",
                "action": "read_repo",
                "intent": "Read repository metadata for triage.",
                "ttl_seconds": 30,
            },
        ),
        (
            "POST",
            "/credential-actions",
            {
                "lease_token": "hvl_demo",
                "action": "read_repo",
                "payload": {"repo": "hivemind"},
            },
        ),
        ("GET", "/tasks", None),
        (
            "POST",
            "/tasks",
            {
                "title": "Review credential policy",
                "description": "Check unauthenticated access handling.",
                "priority": "normal",
                "assigned_agent_id": None,
                "credential_id": None,
                "action": "",
                "intent": "",
                "heartbeat_seconds": None,
            },
        ),
        ("PATCH", "/tasks/task_demo/status", {"status": "running"}),
        ("POST", "/tasks/task_demo/heartbeats", {"note": "still working"}),
        ("GET", "/heartbeats", None),
        ("GET", "/schedules", None),
        (
            "POST",
            "/schedules",
            {
                "name": "Policy review cadence",
                "enabled": True,
                "interval_seconds": 60,
                "task_title": "Scheduled review",
                "task_description": "Check auth boundaries.",
                "priority": "normal",
                "assigned_agent_id": None,
                "credential_id": None,
                "action": "",
                "intent": "",
                "next_run_at": None,
            },
        ),
        ("POST", "/schedules/run-due", None),
        ("GET", "/audit-events", None),
    ]

    for method, path, payload in protected_requests:
        response = client.request(method, path, json=payload)

        require_equal(response.status_code, 401, f"{method} {path} should require authentication")
        require_equal(response.json(), {"detail": "authentication required"}, f"{method} {path} should return a consistent auth error")


def test_create_credential_rejects_invalid_secret_ref(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)

    response = client.post(
        "/credentials",
        json={
            "name": "Bad Credential",
            "provider": "github",
            "secret_ref": "ghp_raw_secret_value",
            "allowed_actions": ["read_repo"],
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "secret_ref must use env://, file://, vault://, or oauth://"


def test_tasks_heartbeats_and_due_schedules(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)
    agent = client.get("/agents").json()[0]

    task_response = client.post(
        "/tasks",
        json={
            "title": "Review credential policy",
            "description": "Confirm the denied paths are tested.",
            "priority": "high",
            "assigned_agent_id": agent["id"],
            "heartbeat_seconds": 60,
        },
    )
    assert task_response.status_code == 201
    task = task_response.json()

    heartbeat = client.post(f"/tasks/{task['id']}/heartbeats", json={"note": "policy review started"})
    assert heartbeat.status_code == 201
    assert client.get("/heartbeats").json()[0]["task_id"] == task["id"]

    schedule_response = client.post(
        "/schedules",
        json={
            "name": "Minute review",
            "interval_seconds": 60,
            "task_title": "Scheduled policy review",
            "assigned_agent_id": agent["id"],
            "next_run_at": "2000-01-01T00:00:00+00:00",
        },
    )
    assert schedule_response.status_code == 201

    run_response = client.post("/schedules/run-due")
    assert run_response.status_code == 200
    assert len(run_response.json()["created_tasks"]) == 1


def test_task_and_schedule_forms_accept_empty_optional_references(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)

    task_response = client.post(
        "/tasks",
        json={
            "title": "Unassigned task",
            "assigned_agent_id": "",
            "credential_id": "",
            "heartbeat_seconds": None,
        },
    )
    assert task_response.status_code == 201
    assert task_response.json()["assigned_agent_id"] is None
    assert task_response.json()["credential_id"] is None

    schedule_response = client.post(
        "/schedules",
        json={
            "name": "Unassigned schedule",
            "interval_seconds": 60,
            "task_title": "Unassigned scheduled task",
            "assigned_agent_id": "",
            "credential_id": "",
        },
    )
    assert schedule_response.status_code == 201
    assert schedule_response.json()["assigned_agent_id"] is None
    assert schedule_response.json()["credential_id"] is None


def test_bad_task_schedule_and_heartbeat_references_return_4xx(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)
    credential = client.get("/credentials").json()[0]

    bad_task_agent = client.post(
        "/tasks",
        json={
            "title": "Broken assignment",
            "assigned_agent_id": "agent_missing",
        },
    )
    assert bad_task_agent.status_code == 400
    assert bad_task_agent.json()["detail"] == "assigned_agent_id references unknown agent: agent_missing"

    bad_task_credential = client.post(
        "/tasks",
        json={
            "title": "Broken credential binding",
            "credential_id": "cred_missing",
        },
    )
    assert bad_task_credential.status_code == 400
    assert bad_task_credential.json()["detail"] == "credential_id references unknown credential: cred_missing"

    bad_schedule_agent = client.post(
        "/schedules",
        json={
            "name": "Broken schedule assignment",
            "interval_seconds": 60,
            "task_title": "Scheduled task",
            "assigned_agent_id": "agent_missing",
        },
    )
    assert bad_schedule_agent.status_code == 400
    assert bad_schedule_agent.json()["detail"] == "assigned_agent_id references unknown agent: agent_missing"

    bad_schedule_credential = client.post(
        "/schedules",
        json={
            "name": "Broken schedule credential",
            "interval_seconds": 60,
            "task_title": "Scheduled task",
            "credential_id": "cred_missing",
        },
    )
    assert bad_schedule_credential.status_code == 400
    assert bad_schedule_credential.json()["detail"] == "credential_id references unknown credential: cred_missing"

    task = client.post(
        "/tasks",
        json={
            "title": "Heartbeat target",
            "credential_id": credential["id"],
        },
    ).json()
    bad_heartbeat_agent = client.post(
        f"/tasks/{task['id']}/heartbeats",
        json={"agent_id": "agent_missing", "note": "still working"},
    )
    assert bad_heartbeat_agent.status_code == 400
    assert bad_heartbeat_agent.json()["detail"] == "agent_id references unknown agent: agent_missing"


def test_existing_email_user_schema_migrates_to_username(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE users (
          id TEXT PRIMARY KEY,
          email TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          role TEXT NOT NULL,
          created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO users (id, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
        ("user_old", "admin@hivemind.local", hash_password(TEST_PASSWORD), "admin", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    client = TestClient(create_app(HivemindStore(db_path), start_scheduler=False))
    response = client.post("/auth/login", json={"username": "admin", "password": TEST_PASSWORD})

    assert response.status_code == 200
    assert response.json()["user"]["username"] == "admin"


def test_existing_plaintext_sessions_migrate_to_token_hashes(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-sessions.db"
    raw_token = secrets.token_urlsafe(24)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE users (
          id TEXT PRIMARY KEY,
          username TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          role TEXT NOT NULL,
          created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE sessions (
          token TEXT PRIMARY KEY,
          user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO users (id, username, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
        ("user_admin", "admin", hash_password(TEST_PASSWORD), "admin", "2026-01-01T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (raw_token, "user_admin", "2026-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    store = HivemindStore(db_path)

    conn = sqlite3.connect(db_path)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(sessions)")]
        session_hash = conn.execute("SELECT token_hash FROM sessions").fetchone()[0]
    finally:
        conn.close()

    require_true("token_hash" in columns, "legacy sessions should migrate to a token_hash column")
    require_true("token" not in columns, "legacy sessions should drop the plaintext token column")
    require_equal(session_hash, store.hash_token(raw_token), "legacy session rows should store hashed tokens")
    session_user = store.get_session_user(raw_token)
    require_true(session_user is not None, "migrated sessions should still authenticate the original cookie token")
    require_equal(session_user.username, "admin", "migrated sessions should keep the original username")
    require_equal(session_user.role, "admin", "migrated sessions should keep the original role")

    store.logout(raw_token)

    conn = sqlite3.connect(db_path)
    try:
        remaining_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    finally:
        conn.close()

    require_equal(remaining_sessions, 0, "logout should remove migrated session rows")


def test_expired_hashed_sessions_are_deleted_on_lookup(tmp_path: Path) -> None:
    db_path = tmp_path / "expired-sessions.db"
    store = HivemindStore(db_path)

    store.setup_admin("admin", TEST_PASSWORD)
    token, _ = store.login("admin", TEST_PASSWORD)
    token_hash = store.hash_token(token)

    with store.connect() as conn:
        conn.execute("UPDATE sessions SET expires_at = ? WHERE token_hash = ?", ("2000-01-01T00:00:00+00:00", token_hash))

    require_equal(store.get_session_user(token), None, "expired sessions should no longer authenticate")

    conn = sqlite3.connect(db_path)
    try:
        remaining_sessions = conn.execute("SELECT COUNT(*) FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone()[0]
    finally:
        conn.close()

    require_equal(remaining_sessions, 0, "expired sessions should be deleted after lookup")
