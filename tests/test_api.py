from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import sqlite3
from threading import Barrier
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

from hivemind.api import create_app
from hivemind.store import HivemindStore, StoreError, hash_password

TEST_PASSWORD = "operator-not-secret"


def client_for(tmp_path: Path, *, base_url: str = "https://testserver") -> TestClient:
    store = HivemindStore(tmp_path / "hivemind.db")
    return TestClient(create_app(store, start_scheduler=False), base_url=base_url)


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
    assert 'id="credential-template-picker"' in response.text
    assert 'id="credential-template-fields"' in response.text
    assert 'name="approval_required_actions"' in response.text
    assert 'id="pending-approvals-list"' in response.text


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


def test_auth_session_cookies_require_https_by_default(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    setup_response = client.post(
        "/auth/setup",
        json={"username": "admin", "password": TEST_PASSWORD},
    )
    logout_response = client.post("/auth/logout")
    login_response = client.post(
        "/auth/login",
        json={"username": "admin", "password": TEST_PASSWORD},
    )

    assert setup_response.status_code == 201
    assert logout_response.status_code == 200
    assert login_response.status_code == 200
    for response in (setup_response, login_response, logout_response):
        set_cookie = response.headers["set-cookie"]
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie
        assert "Secure" in set_cookie


def test_auth_session_cookies_allow_http_in_explicit_development_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HIVEMIND_DEVELOPMENT_MODE", "true")
    client = client_for(tmp_path, base_url="http://testserver")

    setup_response = client.post(
        "/auth/setup",
        json={"username": "admin", "password": TEST_PASSWORD},
    )
    me_response = client.get("/me")
    logout_response = client.post("/auth/logout")
    login_response = client.post(
        "/auth/login",
        json={"username": "admin", "password": TEST_PASSWORD},
    )

    assert setup_response.status_code == 201
    assert me_response.status_code == 200
    assert logout_response.status_code == 200
    assert login_response.status_code == 200
    for response in (setup_response, login_response, logout_response):
        set_cookie = response.headers["set-cookie"]
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie
        assert "Secure" not in set_cookie


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


def test_unknown_lease_agent_is_rejected(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)
    credential = client.get("/credentials").json()[0]

    response = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": "agent_missing",
            "action": "read_repo",
            "intent": "Read repository metadata for safe task triage.",
            "ttl_seconds": 30,
        },
    )

    require_equal(response.status_code, 403, "unknown lease agent should be rejected")
    require_equal(response.json()["detail"], "unknown agent: agent_missing", "lease rejection should explain the missing agent")


def test_guided_github_app_credential_round_trips_public_metadata(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)
    agent = client.get("/agents").json()[0]

    response = client.post(
        "/credentials",
        json={
            "name": "GitHub App Install",
            "provider": "github",
            "secret_ref": "file:///var/lib/hivemind/github-app.pem",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["issue_installation_token", "read_repo"],
            "max_ttl_seconds": 180,
            "require_intent": True,
            "metadata": {
                "credential_kind": "github_app",
                "app_id": "123456",
                "installation_id": "987654321",
            },
        },
    )

    assert response.status_code == 201
    credential = response.json()
    assert credential["provider"] == "github"
    assert credential["metadata"]["credential_kind"] == "github_app"
    assert credential["metadata"]["app_id"] == "123456"
    assert credential["metadata"]["installation_id"] == "987654321"
    assert credential["policy"]["allowed_agents"] == [agent["id"]]
    assert credential["policy"]["approval_required_actions"] == []
    assert credential["secret_ref_preview"].startswith("file://")
    assert "github-app.pem" not in response.text


def test_agent_registry_exposes_lifecycle_and_related_assignments(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)

    create_response = client.post(
        "/agents",
        json={
            "name": "Operator",
            "role": "Own the next swarm task.",
            "provider": "local",
            "model": "deterministic-policy",
            "system_prompt": "Report only the next concrete action.",
        },
    )
    assert create_response.status_code == 201
    agent = create_response.json()
    assert agent["status"] == "idle"
    assert agent["assigned_task_count"] == 0
    assert agent["assigned_schedule_count"] == 0
    assert agent["credential_policy_count"] == 0
    assert agent["assigned_tasks"] == []
    assert agent["assigned_schedules"] == []
    assert agent["credential_policies"] == []

    credential_response = client.post(
        "/credentials",
        json={
            "name": "Scoped Repo Reader",
            "provider": "github",
            "secret_ref": "env://HIVEMIND_DEMO_GITHUB_TOKEN",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["read_repo"],
            "max_ttl_seconds": 60,
            "require_intent": True,
            "metadata": {"credential_kind": "generic_reference"},
        },
    )
    require_equal(credential_response.status_code, 201, "credential creation should succeed before restart")
    credential = credential_response.json()

    task_response = client.post(
        "/tasks",
        json={
            "title": "Inspect repo state",
            "description": "Check the assigned issue branch and report the next action.",
            "assigned_agent_id": agent["id"],
            "heartbeat_seconds": 60,
        },
    )
    require_equal(task_response.status_code, 201, "task creation should succeed before restart")
    task = task_response.json()

    schedule_response = client.post(
        "/schedules",
        json={
            "name": "Hourly repo scan",
            "interval_seconds": 60,
            "task_title": "Scheduled repo scan",
            "assigned_agent_id": agent["id"],
        },
    )
    require_equal(schedule_response.status_code, 201, "schedule creation should succeed before restart")
    schedule = schedule_response.json()

    status_response = client.patch(
        f"/agents/{agent['id']}/status",
        json={"status": "running"},
    )
    assert status_response.status_code == 200
    updated = status_response.json()
    assert updated["status"] == "running"
    assert updated["assigned_task_count"] == 1
    assert updated["active_task_count"] == 1
    assert updated["assigned_schedule_count"] == 1
    assert updated["credential_policy_count"] == 1
    assert updated["assigned_tasks"] == [
        {
            "id": task["id"],
            "title": "Inspect repo state",
            "status": "queued",
            "priority": "normal",
            "updated_at": task["updated_at"],
        }
    ]
    assert updated["assigned_schedules"] == [
        {
            "id": schedule["id"],
            "name": "Hourly repo scan",
            "enabled": True,
            "interval_seconds": 60,
            "next_run_at": schedule["next_run_at"],
            "task_title": "Scheduled repo scan",
        }
    ]
    assert updated["credential_policies"] == [
        {
            "id": credential["id"],
            "name": "Scoped Repo Reader",
            "provider": "github",
            "allowed_actions": ["read_repo"],
            "max_ttl_seconds": 60,
            "require_intent": True,
        }
    ]

    listed_agents = {item["id"]: item for item in client.get("/agents").json()}
    assert listed_agents[agent["id"]]["status"] == "running"
    assert listed_agents[agent["id"]]["assigned_task_count"] == 1


def test_unknown_agent_status_update_returns_404(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)

    response = client.patch("/agents/agent_missing/status", json={"status": "blocked"})

    assert response.status_code == 404
    assert response.json()["detail"] == "unknown agent: agent_missing"


def test_legacy_working_agent_status_alias_is_normalized(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)

    create_response = client.post(
        "/agents",
        json={
            "name": "Operator",
            "role": "Own the next swarm task.",
            "provider": "local",
            "model": "deterministic-policy",
            "system_prompt": "Report only the next concrete action.",
        },
    )
    assert create_response.status_code == 201
    agent = create_response.json()

    response = client.patch(
        f"/agents/{agent['id']}/status",
        json={"status": "working"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "running"


def test_agents_persist_across_store_restart(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)

    create_response = client.post(
        "/agents",
        json={
            "name": "Operator",
            "role": "Own the next swarm task.",
            "provider": "local",
            "model": "deterministic-policy",
            "system_prompt": "Report only the next concrete action.",
        },
    )
    assert create_response.status_code == 201
    agent = create_response.json()

    credential_response = client.post(
        "/credentials",
        json={
            "name": "Scoped Repo Reader",
            "provider": "github",
            "secret_ref": "env://HIVEMIND_DEMO_GITHUB_TOKEN",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["read_repo"],
            "max_ttl_seconds": 60,
            "require_intent": True,
            "metadata": {"credential_kind": "generic_reference"},
        },
    )
    require_equal(credential_response.status_code, 201, "credential creation should succeed before restart")
    credential = credential_response.json()

    task_response = client.post(
        "/tasks",
        json={
            "title": "Inspect repo state",
            "description": "Check the assigned issue branch and report the next action.",
            "assigned_agent_id": agent["id"],
            "heartbeat_seconds": 60,
        },
    )
    require_equal(task_response.status_code, 201, "task creation should succeed before restart")
    task = task_response.json()

    schedule_response = client.post(
        "/schedules",
        json={
            "name": "Hourly repo scan",
            "interval_seconds": 60,
            "task_title": "Scheduled repo scan",
            "assigned_agent_id": agent["id"],
        },
    )
    require_equal(schedule_response.status_code, 201, "schedule creation should succeed before restart")
    schedule = schedule_response.json()

    update_response = client.patch(
        f"/agents/{agent['id']}/status",
        json={"status": "running"},
    )
    require_equal(update_response.status_code, 200, "agent status update should succeed before restart")

    restarted_client = client_for(tmp_path)
    login_response = restarted_client.post(
        "/auth/login",
        json={"username": "admin", "password": TEST_PASSWORD},
    )
    require_equal(login_response.status_code, 200, "login should succeed after restart")

    agents = {item["id"]: item for item in restarted_client.get("/agents").json()}
    require_equal(agents[agent["id"]]["name"], "Operator", "agent name should persist across restart")
    require_equal(agents[agent["id"]]["status"], "running", "agent status should persist across restart")
    require_equal(agents[agent["id"]]["assigned_task_count"], 1, "assigned task count should persist across restart")
    require_equal(agents[agent["id"]]["active_task_count"], 1, "active task count should persist across restart")
    require_equal(agents[agent["id"]]["assigned_schedule_count"], 1, "assigned schedule count should persist across restart")
    require_equal(agents[agent["id"]]["credential_policy_count"], 1, "credential policy count should persist across restart")
    require_equal(
        agents[agent["id"]]["assigned_tasks"],
        [
        {
            "id": task["id"],
            "title": "Inspect repo state",
            "status": "queued",
            "priority": "normal",
            "updated_at": task["updated_at"],
        }
        ],
        "assigned task rollups should persist across restart",
    )
    require_equal(
        agents[agent["id"]]["assigned_schedules"],
        [
        {
            "id": schedule["id"],
            "name": "Hourly repo scan",
            "enabled": True,
            "interval_seconds": 60,
            "next_run_at": schedule["next_run_at"],
            "task_title": "Scheduled repo scan",
        }
        ],
        "assigned schedule rollups should persist across restart",
    )
    require_equal(
        agents[agent["id"]]["credential_policies"],
        [
        {
            "id": credential["id"],
            "name": "Scoped Repo Reader",
            "provider": "github",
            "allowed_actions": ["read_repo"],
            "max_ttl_seconds": 60,
            "require_intent": True,
        }
        ],
        "credential policy rollups should persist across restart",
    )


def test_credential_rejects_unknown_allowed_agent(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)

    response = client.post(
        "/credentials",
        json={
            "name": "Scoped Repo Reader",
            "provider": "github",
            "secret_ref": "env://HIVEMIND_DEMO_GITHUB_TOKEN",
            "allowed_agents": ["agent_missing"],
            "allowed_actions": ["read_repo"],
            "max_ttl_seconds": 60,
            "require_intent": True,
            "metadata": {"credential_kind": "generic_reference"},
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "allowed_agents references unknown agent: agent_missing"


def test_oauth_credential_rejects_unknown_allowed_agent_on_callback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HIVEMIND_SECRETS_KEY", "local-test-secret-key")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_AUTHORIZE_URL", "https://auth.example.test/oauth/authorize")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_TOKEN_URL", "https://auth.example.test/oauth/token")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_CLIENT_ID", "codex-client")

    class FakeTokenResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "access_token": "access-secret-token",
                "refresh_token": "refresh-secret-token",
                "scope": "openid offline_access",
                "expires_in": 1800,
                "token_type": "Bearer",
            }

    def fake_post(url: str, *, data: dict[str, str], headers: dict[str, str], timeout: float) -> FakeTokenResponse:
        return FakeTokenResponse()

    monkeypatch.setattr(httpx, "post", fake_post)

    client = client_for(tmp_path)
    setup(client)

    start_response = client.post(
        "/oauth/credentials/start",
        json={
            "provider": "codex",
            "name": "codex subscription",
            "allowed_agents": ["agent_missing"],
            "allowed_actions": ["delegate_code"],
            "max_ttl_seconds": 900,
            "require_intent": True,
        },
    )
    require_equal(start_response.status_code, 201, "oauth credential start should succeed before callback validation")
    authorize_url = start_response.json()["authorize_url"]
    query = parse_qs(urlparse(authorize_url).query)

    callback_response = client.get(
        f"/oauth/callback/codex?state={query['state'][0]}&code=broker-code",
        follow_redirects=False,
    )

    require_equal(callback_response.status_code, 303, "oauth callback should redirect after rejecting an unknown allowed agent")
    redirect_params = parse_qs(urlparse(callback_response.headers["location"]).query)
    require_equal(redirect_params["oauth"], ["error"], "oauth callback should report an error status")
    require_equal(
        redirect_params["detail"],
        ["allowed_agents references unknown agent: agent_missing"],
        "oauth callback should explain the unknown allowed agent",
    )
    audit_events = client.get("/audit-events").json()
    require_equal(audit_events[0]["type"], "credential.oauth.failed", "oauth failure should be audited")
    require_equal(
        audit_events[0]["reason"],
        "allowed_agents references unknown agent: agent_missing",
        "oauth audit reason should explain the unknown allowed agent",
    )
    credentials = client.get("/credentials").json()
    require_true(
        all(item["provider"] != "codex" for item in credentials),
        "oauth callback should not create a codex credential for an unknown allowed agent",
    )


def test_approval_required_lease_flow_requires_operator_decision(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)
    agent = client.get("/agents").json()[0]

    credential_response = client.post(
        "/credentials",
        json={
            "name": "GitHub Writer",
            "provider": "github",
            "secret_ref": "env://GITHUB_WRITE_TOKEN",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["open_issue", "read_repo"],
            "approval_required_actions": ["open_issue"],
            "max_ttl_seconds": 180,
            "require_intent": True,
            "metadata": {"credential_kind": "generic_reference"},
        },
    )

    assert credential_response.status_code == 201
    credential = credential_response.json()
    assert credential["policy"]["approval_required_actions"] == ["open_issue"]

    pending_response = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": agent["id"],
            "action": "open_issue",
            "intent": "Open a verified issue for a reproducible credential broker regression.",
            "ttl_seconds": 600,
        },
    )

    assert pending_response.status_code == 201
    pending_lease = pending_response.json()
    assert pending_lease["status"] == "pending"
    assert pending_lease["token_preview"] == "not issued"
    assert pending_lease["ttl_seconds"] == 180
    assert "lease_token" not in pending_lease

    listed_pending = client.get("/credential-leases").json()
    assert any(item["id"] == pending_lease["id"] and item["status"] == "pending" for item in listed_pending)

    approve_response = client.post(f"/credential-leases/{pending_lease['id']}/approve")
    assert approve_response.status_code == 200
    approved_lease = approve_response.json()
    assert approved_lease["status"] == "active"
    assert approved_lease["ttl_seconds"] == 180
    assert approved_lease["lease_token"].startswith("hvl_")

    action_response = client.post(
        "/credential-actions",
        json={
            "lease_token": approved_lease["lease_token"],
            "action": "open_issue",
            "payload": {"repo": "hivemind", "title": "credential approval regression"},
        },
    )
    assert action_response.status_code == 200
    assert action_response.json()["ok"] is True

    denied_pending = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": agent["id"],
            "action": "open_issue",
            "intent": "Open a second issue to confirm operator denial paths stay audited.",
            "ttl_seconds": 90,
        },
    ).json()
    deny_response = client.post(f"/credential-leases/{denied_pending['id']}/deny")
    assert deny_response.status_code == 200
    denied_lease = deny_response.json()
    assert denied_lease["status"] == "denied"
    assert denied_lease["token_preview"] == "not issued"
    assert "lease_token" not in denied_lease

    audit_events = client.get("/audit-events").json()
    assert any(
        event["type"] == "credential.lease.pending"
        and event["metadata"]["lease_id"] == pending_lease["id"]
        and event["decision"] == "pending"
        for event in audit_events
    )
    assert any(
        event["type"] == "credential.lease.approved"
        and event["metadata"]["lease_id"] == pending_lease["id"]
        and event["decision"] == "allowed"
        for event in audit_events
    )
    assert any(
        event["type"] == "credential.lease.denied"
        and event["metadata"]["lease_id"] == denied_pending["id"]
        and event["reason"] == "operator denied lease request"
        for event in audit_events
    )


def test_persisted_pending_and_denied_lease_tokens_cannot_perform_actions(tmp_path: Path) -> None:
    db_path = tmp_path / "persisted-approval-status.db"
    store = HivemindStore(db_path)
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup(client)
    agent = client.get("/agents").json()[0]
    secret_ref_value = f"env://GITHUB_WRITE_{secrets.token_hex(4).upper()}"
    credential = client.post(
        "/credentials",
        json={
            "name": "GitHub Writer",
            "provider": "github",
            "secret_ref": secret_ref_value,
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["open_issue"],
            "approval_required_actions": ["open_issue"],
            "max_ttl_seconds": 60,
            "require_intent": True,
            "metadata": {"credential_kind": "generic_reference"},
        },
    ).json()
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(seconds=60)
    lease_values = {
        "pending": f"hvp_{secrets.token_urlsafe(18)}",
        "denied": f"hvp_{secrets.token_urlsafe(18)}",
    }

    with store.connect() as conn:
        for status, lease_secret in lease_values.items():
            conn.execute(
                """
                INSERT INTO leases
                (
                  id, token_hash, token_preview, credential_id, agent_id,
                  action, intent, ttl_seconds, status, issued_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"lease_{status}_known_hash",
                    store.hash_token(lease_secret),
                    "not issued",
                    credential["id"],
                    agent["id"],
                    "open_issue",
                    "Confirm persisted approval status cannot be bypassed with a matching lease hash.",
                    60,
                    status,
                    issued_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )

    pending_response = client.post(
        "/credential-actions",
        json={
            "lease_token": lease_values["pending"],
            "action": "open_issue",
            "payload": {"repo": "hivemind"},
        },
    )
    denied_response = client.post(
        "/credential-actions",
        json={
            "lease_token": lease_values["denied"],
            "action": "open_issue",
            "payload": {"repo": "hivemind"},
        },
    )

    require_equal(pending_response.status_code, 403, "pending persisted leases should reject credential actions")
    require_equal(
        pending_response.json()["detail"],
        "credential lease is pending approval",
        "pending lease rejection should explain approval state",
    )
    require_equal(denied_response.status_code, 403, "denied persisted leases should reject credential actions")
    require_equal(
        denied_response.json()["detail"],
        "credential lease request was denied",
        "denied lease rejection should explain denial state",
    )


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
        ("PATCH", "/agents/agent_demo/status", {"status": "running"}),
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
        ("POST", "/credential-leases/lease_demo/approve", None),
        ("POST", "/credential-leases/lease_demo/deny", None),
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
        ("PATCH", "/schedules/sched_demo", {"enabled": False}),
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


def test_guided_github_credential_metadata_is_validated(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)

    oauth_response = client.post(
        "/credentials",
        json={
            "name": "Broken OAuth App",
            "provider": "github",
            "secret_ref": "file:///var/lib/hivemind/github-oauth-app.ref",
            "allowed_actions": ["exchange_oauth_code"],
            "metadata": {"credential_kind": "github_oauth_app"},
        },
    )
    assert oauth_response.status_code == 400
    assert oauth_response.json()["detail"] == "github_oauth_app metadata requires client_id"

    app_response = client.post(
        "/credentials",
        json={
            "name": "Broken GitHub App",
            "provider": "github",
            "secret_ref": "file:///var/lib/hivemind/github-app.pem",
            "allowed_actions": ["issue_installation_token"],
            "metadata": {"credential_kind": "github_app", "installation_id": "987654321"},
        },
    )
    assert app_response.status_code == 400
    assert app_response.json()["detail"] == "github_app metadata requires app_id"


def test_oauth_provider_status_reports_missing_broker_secret_store(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)

    response = client.get("/oauth/providers")

    assert response.status_code == 200
    provider = response.json()[0]
    assert provider["id"] == "codex"
    assert provider["available"] is False
    assert "HIVEMIND_SECRETS_KEY" in provider["reason"]


def test_codex_oauth_flow_creates_redacted_credential_and_encrypts_tokens(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HIVEMIND_SECRETS_KEY", "local-test-secret-key")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_AUTHORIZE_URL", "https://auth.example.test/oauth/authorize")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_TOKEN_URL", "https://auth.example.test/oauth/token")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_CLIENT_ID", "codex-client")

    captured: dict[str, object] = {}

    class FakeTokenResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "access_token": "access-secret-token",
                "refresh_token": "refresh-secret-token",
                "scope": "openid offline_access",
                "expires_in": 1800,
                "token_type": "Bearer",
            }

    def fake_post(url: str, *, data: dict[str, str], headers: dict[str, str], timeout: float) -> FakeTokenResponse:
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        captured["timeout"] = timeout
        return FakeTokenResponse()

    monkeypatch.setattr(httpx, "post", fake_post)

    client = client_for(tmp_path)
    setup(client)
    agent = client.get("/agents").json()[0]

    start_response = client.post(
        "/oauth/credentials/start",
        json={
            "provider": "codex",
            "name": "codex subscription",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["delegate_code", "review_code"],
            "max_ttl_seconds": 900,
            "require_intent": True,
        },
    )

    assert start_response.status_code == 201
    authorize_url = start_response.json()["authorize_url"]
    parsed = urlparse(authorize_url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.example.test"
    assert query["client_id"] == ["codex-client"]
    assert query["scope"] == ["openid profile email offline_access"]
    assert "state" in query
    assert "code_challenge" in query

    callback_response = client.get(
        f"/oauth/callback/codex?state={query['state'][0]}&code=broker-code",
        follow_redirects=False,
    )

    assert callback_response.status_code == 303
    assert callback_response.headers["location"].startswith("/?oauth=connected")

    credentials = client.get("/credentials").json()
    codex_credential = next(item for item in credentials if item["provider"] == "codex")
    assert codex_credential["name"] == "codex subscription"
    assert codex_credential["secret_ref_preview"] == "oauth://cod..."
    assert codex_credential["metadata"]["auth_type"] == "oauth"
    assert codex_credential["metadata"]["oauth_refreshable"] is True
    assert "access-secret-token" not in start_response.text
    assert "access-secret-token" not in callback_response.text
    assert "refresh-secret-token" not in callback_response.text

    audit_events = client.get("/audit-events").json()
    assert audit_events[0]["type"] == "credential.oauth.connected"

    conn = sqlite3.connect(tmp_path / "hivemind.db")
    token_row = conn.execute("SELECT token_ciphertext FROM oauth_connections").fetchone()
    conn.close()
    assert token_row is not None
    assert "access-secret-token" not in token_row[0]
    assert "refresh-secret-token" not in token_row[0]
    assert captured["url"] == "https://auth.example.test/oauth/token"
    assert captured["data"]["code"] == "broker-code"
    assert captured["data"]["client_id"] == "codex-client"
    assert captured["data"]["grant_type"] == "authorization_code"
    assert "code_verifier" in captured["data"]


def test_codex_oauth_flow_rejects_non_object_token_response(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HIVEMIND_SECRETS_KEY", "local-test-secret-key")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_AUTHORIZE_URL", "https://auth.example.test/oauth/authorize")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_TOKEN_URL", "https://auth.example.test/oauth/token")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_CLIENT_ID", "codex-client")

    class FakeTokenResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[str]:
            return ["not", "an", "object"]

    def fake_post(url: str, *, data: dict[str, str], headers: dict[str, str], timeout: float) -> FakeTokenResponse:
        return FakeTokenResponse()

    monkeypatch.setattr(httpx, "post", fake_post)

    client = client_for(tmp_path)
    setup(client)
    agent = client.get("/agents").json()[0]

    start_response = client.post(
        "/oauth/credentials/start",
        json={
            "provider": "codex",
            "name": "codex subscription",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["delegate_code", "review_code"],
            "max_ttl_seconds": 900,
            "require_intent": True,
        },
    )
    assert start_response.status_code == 201
    authorize_url = start_response.json()["authorize_url"]
    query = parse_qs(urlparse(authorize_url).query)

    callback_response = client.get(
        f"/oauth/callback/codex?state={query['state'][0]}&code=broker-code",
        follow_redirects=False,
    )

    assert callback_response.status_code == 303
    redirect_params = parse_qs(urlparse(callback_response.headers["location"]).query)
    assert redirect_params["oauth"] == ["error"]
    assert redirect_params["detail"] == ["oauth token response must be a JSON object"]
    audit_events = client.get("/audit-events").json()
    assert audit_events[0]["type"] == "credential.oauth.failed"
    assert audit_events[0]["reason"] == "oauth token response must be a JSON object"
    credentials = client.get("/credentials").json()
    assert all(item["provider"] != "codex" for item in credentials)


def test_codex_oauth_flow_audits_unknown_state_callback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HIVEMIND_SECRETS_KEY", "local-test-secret-key")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_AUTHORIZE_URL", "https://auth.example.test/oauth/authorize")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_TOKEN_URL", "https://auth.example.test/oauth/token")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_CLIENT_ID", "codex-client")

    client = client_for(tmp_path)
    setup(client)

    callback_response = client.get(
        "/oauth/callback/codex?state=oauth_state_missing&code=broker-code",
        follow_redirects=False,
    )

    assert callback_response.status_code == 303
    redirect_params = parse_qs(urlparse(callback_response.headers["location"]).query)
    assert redirect_params["oauth"] == ["error"]
    assert redirect_params["detail"] == ["unknown oauth state"]
    audit_events = client.get("/audit-events").json()
    assert audit_events[0]["type"] == "credential.oauth.failed"
    assert audit_events[0]["reason"] == "unknown oauth state"
    assert audit_events[0]["target_id"] == "codex"


def test_codex_oauth_flow_audits_missing_code_callback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HIVEMIND_SECRETS_KEY", "local-test-secret-key")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_AUTHORIZE_URL", "https://auth.example.test/oauth/authorize")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_TOKEN_URL", "https://auth.example.test/oauth/token")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_CLIENT_ID", "codex-client")

    client = client_for(tmp_path)
    setup(client)
    agent = client.get("/agents").json()[0]

    start_response = client.post(
        "/oauth/credentials/start",
        json={
            "provider": "codex",
            "name": "codex subscription",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["delegate_code", "review_code"],
            "max_ttl_seconds": 900,
            "require_intent": True,
        },
    )
    assert start_response.status_code == 201
    authorize_url = start_response.json()["authorize_url"]
    query = parse_qs(urlparse(authorize_url).query)

    callback_response = client.get(
        f"/oauth/callback/codex?state={query['state'][0]}",
        follow_redirects=False,
    )

    assert callback_response.status_code == 303
    redirect_params = parse_qs(urlparse(callback_response.headers["location"]).query)
    assert redirect_params["oauth"] == ["error"]
    assert redirect_params["detail"] == ["Missing OAuth authorization code."]
    audit_events = client.get("/audit-events").json()
    assert audit_events[0]["type"] == "credential.oauth.failed"
    assert audit_events[0]["reason"] == "Missing OAuth authorization code."
    assert audit_events[0]["target_id"] == "codex"
    credentials = client.get("/credentials").json()
    assert all(item["provider"] != "codex" for item in credentials)


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
    schedule = schedule_response.json()
    assert schedule["enabled"] is True

    disable_response = client.patch(f"/schedules/{schedule['id']}", json={"enabled": False})
    assert disable_response.status_code == 200
    assert disable_response.json()["id"] == schedule["id"]
    assert disable_response.json()["enabled"] is False

    paused_run_response = client.post("/schedules/run-due")
    assert paused_run_response.status_code == 200
    assert paused_run_response.json()["created_tasks"] == []

    resumed_schedule = client.patch(f"/schedules/{schedule['id']}", json={"enabled": True})
    assert resumed_schedule.status_code == 200
    assert resumed_schedule.json()["id"] == schedule["id"]
    assert resumed_schedule.json()["enabled"] is True

    resumed_run_response = client.post("/schedules/run-due")
    assert resumed_run_response.status_code == 200
    created_tasks = resumed_run_response.json()["created_tasks"]
    assert len(created_tasks) == 1
    assert created_tasks[0]["title"] == "Scheduled policy review"

    second_paused_run_response = client.post("/schedules/run-due")
    assert second_paused_run_response.status_code == 200
    assert second_paused_run_response.json()["created_tasks"] == []

    schedules = client.get("/schedules")
    assert schedules.status_code == 200
    current_schedule = next(item for item in schedules.json() if item["id"] == schedule["id"])
    assert current_schedule["enabled"] is True
    assert current_schedule["last_run_at"] is not None

    audit_events = client.get("/audit-events")
    assert audit_events.status_code == 200
    schedule_run_event = next(event for event in audit_events.json() if event["type"] == "schedule.ran")
    assert schedule_run_event["actor_id"] == agent["id"]
    assert schedule_run_event["target_id"] == schedule["id"]
    assert schedule_run_event["decision"] == "allowed"
    assert schedule_run_event["reason"] == "scheduled task created"
    assert schedule_run_event["metadata"]["task_id"] == created_tasks[0]["id"]


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
    agents = client.get("/agents").json()
    primary_agent = agents[0]
    secondary_agent = client.post(
        "/agents",
        json={
            "name": "Mismatched heartbeat worker",
            "role": "Try to post a heartbeat onto the wrong task.",
            "provider": "local",
            "model": "deterministic-policy",
            "system_prompt": "Report only the next concrete action.",
        },
    ).json()
    credential = client.get("/credentials").json()[0]

    bad_task_agent = client.post(
        "/tasks",
        json={
            "title": "Broken assignment",
            "assigned_agent_id": "agent_missing",
        },
    )
    require_equal(bad_task_agent.status_code, 400, "unknown task agent should be rejected")
    require_equal(
        bad_task_agent.json()["detail"],
        "assigned_agent_id references unknown agent: agent_missing",
        "task agent rejection should explain the missing reference",
    )

    bad_task_credential = client.post(
        "/tasks",
        json={
            "title": "Broken credential binding",
            "credential_id": "cred_missing",
        },
    )
    require_equal(bad_task_credential.status_code, 400, "unknown task credential should be rejected")
    require_equal(
        bad_task_credential.json()["detail"],
        "credential_id references unknown credential: cred_missing",
        "task credential rejection should explain the missing reference",
    )

    bad_schedule_agent = client.post(
        "/schedules",
        json={
            "name": "Broken schedule assignment",
            "interval_seconds": 60,
            "task_title": "Scheduled task",
            "assigned_agent_id": "agent_missing",
        },
    )
    require_equal(bad_schedule_agent.status_code, 400, "unknown schedule agent should be rejected")
    require_equal(
        bad_schedule_agent.json()["detail"],
        "assigned_agent_id references unknown agent: agent_missing",
        "schedule agent rejection should explain the missing reference",
    )

    bad_schedule_credential = client.post(
        "/schedules",
        json={
            "name": "Broken schedule credential",
            "interval_seconds": 60,
            "task_title": "Scheduled task",
            "credential_id": "cred_missing",
        },
    )
    require_equal(bad_schedule_credential.status_code, 400, "unknown schedule credential should be rejected")
    require_equal(
        bad_schedule_credential.json()["detail"],
        "credential_id references unknown credential: cred_missing",
        "schedule credential rejection should explain the missing reference",
    )

    scoped_credential_response = client.post(
        "/credentials",
        json={
            "name": "Scoped Repo Reader",
            "provider": "github",
            "secret_ref": "env://HIVEMIND_DEMO_GITHUB_TOKEN",
            "allowed_agents": [primary_agent["id"]],
            "allowed_actions": ["read_repo"],
            "max_ttl_seconds": 60,
            "require_intent": True,
            "metadata": {"credential_kind": "generic_reference"},
        },
    )
    require_equal(scoped_credential_response.status_code, 201, "scoped credential should be created for the allowed primary agent")
    scoped_credential = scoped_credential_response.json()

    bad_task_binding = client.post(
        "/tasks",
        json={
            "title": "Forbidden credential binding",
            "assigned_agent_id": secondary_agent["id"],
            "credential_id": scoped_credential["id"],
        },
    )
    require_equal(bad_task_binding.status_code, 400, "task agent/credential policy mismatch should be rejected")
    require_equal(
        bad_task_binding.json()["detail"],
        f"assigned_agent_id is not allowed to use credential {scoped_credential['id']}: {secondary_agent['id']}",
        "task agent/credential rejection should explain the disallowed binding",
    )

    bad_schedule_binding = client.post(
        "/schedules",
        json={
            "name": "Forbidden schedule credential binding",
            "interval_seconds": 60,
            "task_title": "Scheduled forbidden binding",
            "assigned_agent_id": secondary_agent["id"],
            "credential_id": scoped_credential["id"],
        },
    )
    require_equal(bad_schedule_binding.status_code, 400, "schedule agent/credential policy mismatch should be rejected")
    require_equal(
        bad_schedule_binding.json()["detail"],
        f"assigned_agent_id is not allowed to use credential {scoped_credential['id']}: {secondary_agent['id']}",
        "schedule agent/credential rejection should explain the disallowed binding",
    )

    task = client.post(
        "/tasks",
        json={
            "title": "Heartbeat target",
            "assigned_agent_id": primary_agent["id"],
            "credential_id": credential["id"],
        },
    ).json()
    bad_heartbeat_agent = client.post(
        f"/tasks/{task['id']}/heartbeats",
        json={"agent_id": "agent_missing", "note": "still working"},
    )
    require_equal(bad_heartbeat_agent.status_code, 400, "unknown heartbeat agent should be rejected")
    require_equal(
        bad_heartbeat_agent.json()["detail"],
        "agent_id references unknown agent: agent_missing",
        "heartbeat rejection should explain the missing agent reference",
    )

    mismatched_heartbeat_agent = client.post(
        f"/tasks/{task['id']}/heartbeats",
        json={"agent_id": secondary_agent["id"], "note": "still working"},
    )
    require_equal(mismatched_heartbeat_agent.status_code, 400, "mismatched heartbeat agent should be rejected")
    require_equal(
        mismatched_heartbeat_agent.json()["detail"],
        f"agent_id does not match assigned agent for task {task['id']}: {secondary_agent['id']}",
        "heartbeat rejection should explain the assigned-agent mismatch",
    )


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
