from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
    assert 'id="credential-template-picker"' in response.text
    assert 'id="credential-template-fields"' in response.text


def test_frontend_renders_task_operator_controls(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    for required in [
        'id="running-task-count"',
        'id="blocked-task-count"',
        'id="due-schedule-count"',
        'id="stale-heartbeat-count"',
        'id="task-health"',
        'name="status"',
    ]:
        assert required in response.text


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
    assert credential["secret_ref_preview"].startswith("file://")
    assert "github-app.pem" not in response.text


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
        (
            "PATCH",
            "/tasks/task_demo",
            {
                "title": "Retitle task",
                "description": "Unauthorized edit attempt.",
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
    credential = client.get("/credentials").json()[0]

    task_response = client.post(
        "/tasks",
        json={
            "title": "Review credential policy",
            "description": "Confirm the denied paths are tested.",
            "priority": "urgent",
            "assigned_agent_id": agent["id"],
            "credential_id": credential["id"],
            "action": "read_repo",
            "intent": "Review the repo policy path and report blockers.",
            "heartbeat_seconds": 60,
        },
    )
    assert task_response.status_code == 201
    task = task_response.json()
    assert task["status"] == "queued"
    assert task["priority"] == "urgent"
    assert task["credential_id"] == credential["id"]

    task_list = client.get("/tasks")
    assert task_list.status_code == 200
    assert task_list.json()[0]["id"] == task["id"]

    task_status = client.patch(f"/tasks/{task['id']}/status", json={"status": "blocked"})
    assert task_status.status_code == 200
    assert task_status.json()["status"] == "blocked"

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

    audit_types = [event["type"] for event in client.get("/audit-events").json()]
    assert "task.created" in audit_types
    assert "task.status.updated" in audit_types


def test_task_management_flow_exposes_create_list_status_and_audit_state(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)
    me = client.get("/me").json()
    agent = client.get("/agents").json()[0]
    credential = client.get("/credentials").json()[0]

    create_response = client.post(
        "/tasks",
        json={
            "title": "Review audit visibility",
            "description": "Verify task transitions remain visible in the operator API.",
            "priority": "urgent",
            "assigned_agent_id": agent["id"],
            "credential_id": credential["id"],
            "action": "read_repo",
            "intent": "Inspect the task-management API acceptance surface.",
            "heartbeat_seconds": 90,
        },
    )

    assert create_response.status_code == 201
    created_task = create_response.json()
    assert created_task["title"] == "Review audit visibility"
    assert created_task["description"] == "Verify task transitions remain visible in the operator API."
    assert created_task["status"] == "queued"
    assert created_task["priority"] == "urgent"
    assert created_task["assigned_agent_id"] == agent["id"]
    assert created_task["credential_id"] == credential["id"]
    assert created_task["action"] == "read_repo"
    assert created_task["intent"] == "Inspect the task-management API acceptance surface."
    assert created_task["heartbeat_seconds"] == 90
    assert created_task["next_heartbeat_at"] is not None
    assert created_task["created_at"] == created_task["updated_at"]

    list_response = client.get("/tasks")
    assert list_response.status_code == 200
    assert list_response.json() == [created_task]

    status_response = client.patch(f"/tasks/{created_task['id']}/status", json={"status": "running"})
    assert status_response.status_code == 200
    updated_task = status_response.json()
    assert updated_task["id"] == created_task["id"]
    assert updated_task["status"] == "running"
    assert updated_task["updated_at"] != created_task["updated_at"]
    assert updated_task["created_at"] == created_task["created_at"]

    listed_after_update = client.get("/tasks")
    assert listed_after_update.status_code == 200
    assert listed_after_update.json() == [updated_task]

    heartbeat_response = client.post(
        f"/tasks/{created_task['id']}/heartbeats",
        json={"note": "operator verified the task is active"},
    )
    assert heartbeat_response.status_code == 201
    heartbeat = heartbeat_response.json()
    assert heartbeat["task_id"] == created_task["id"]
    assert heartbeat["agent_id"] == agent["id"]
    assert heartbeat["note"] == "operator verified the task is active"

    heartbeats_response = client.get(f"/heartbeats?task_id={created_task['id']}")
    assert heartbeats_response.status_code == 200
    assert heartbeats_response.json() == [heartbeat]

    audit_response = client.get("/audit-events")
    assert audit_response.status_code == 200
    audit_events = audit_response.json()
    task_audit_events = [event for event in audit_events if event["target_id"] == created_task["id"]]

    assert [event["type"] for event in task_audit_events] == [
        "task.heartbeat",
        "task.status.updated",
        "task.created",
    ]
    assert task_audit_events[0]["actor_id"] == me["id"]
    assert task_audit_events[0]["decision"] == "allowed"
    assert task_audit_events[0]["reason"] == "heartbeat recorded"
    assert task_audit_events[0]["metadata"] == {
        "agent_id": agent["id"],
        "note": "operator verified the task is active",
    }
    assert task_audit_events[1]["actor_id"] == me["id"]
    assert task_audit_events[1]["decision"] == "allowed"
    assert task_audit_events[1]["reason"] == "task marked running"
    assert task_audit_events[1]["metadata"] == {"from_status": "queued", "to_status": "running"}
    assert task_audit_events[2]["actor_id"] == me["id"]
    assert task_audit_events[2]["decision"] == "allowed"
    assert task_audit_events[2]["reason"] == "task created"
    assert task_audit_events[2]["metadata"] == {
        "status": "queued",
        "priority": "urgent",
        "assigned_agent_id": agent["id"],
        "credential_id": credential["id"],
        "action": "read_repo",
        "intent": "Inspect the task-management API acceptance surface.",
        "heartbeat_seconds": 90,
    }


def test_task_management_allows_non_status_updates_via_task_api(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)
    agent = client.get("/agents").json()[0]
    credential = client.get("/credentials").json()[0]

    created_task = client.post(
        "/tasks",
        json={
            "title": "Original task",
            "description": "Original description",
            "priority": "normal",
            "assigned_agent_id": agent["id"],
            "credential_id": credential["id"],
            "action": "read_repo",
            "intent": "Capture the initial task payload.",
            "heartbeat_seconds": 60,
        },
    ).json()

    update_response = client.patch(
        f"/tasks/{created_task['id']}",
        json={
            "title": "Updated task",
            "description": "Updated description",
            "priority": "high",
            "assigned_agent_id": "",
            "credential_id": "",
            "action": "review_code",
            "intent": "Verify the task update contract.",
            "heartbeat_seconds": 120,
        },
    )

    assert update_response.status_code == 200
    updated_task = update_response.json()
    assert updated_task["id"] == created_task["id"]
    assert updated_task["title"] == "Updated task"
    assert updated_task["description"] == "Updated description"
    assert updated_task["status"] == "queued"
    assert updated_task["priority"] == "high"
    assert updated_task["assigned_agent_id"] is None
    assert updated_task["credential_id"] is None
    assert updated_task["action"] == "review_code"
    assert updated_task["intent"] == "Verify the task update contract."
    assert updated_task["heartbeat_seconds"] == 120
    assert updated_task["next_heartbeat_at"] is not None
    assert updated_task["created_at"] == created_task["created_at"]
    assert updated_task["updated_at"] != created_task["updated_at"]

    listed_tasks = client.get("/tasks")
    assert listed_tasks.status_code == 200
    assert listed_tasks.json() == [updated_task]


def test_task_update_rejects_null_title_and_clears_optional_text_fields(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)

    created_task = client.post(
        "/tasks",
        json={
            "title": "Mutable task",
            "description": "Needs cleanup",
            "action": "read_repo",
            "intent": "Review the mutable task path.",
            "heartbeat_seconds": 60,
        },
    ).json()

    null_title_response = client.patch(
        f"/tasks/{created_task['id']}",
        json={"title": None},
    )
    assert null_title_response.status_code == 400
    assert null_title_response.json()["detail"] == "title must not be null"

    cleared_response = client.patch(
        f"/tasks/{created_task['id']}",
        json={
            "description": None,
            "action": None,
            "intent": None,
            "heartbeat_seconds": None,
        },
    )
    assert cleared_response.status_code == 200
    cleared_task = cleared_response.json()
    assert cleared_task["description"] == ""
    assert cleared_task["action"] == ""
    assert cleared_task["intent"] == ""
    assert cleared_task["heartbeat_seconds"] is None
    assert cleared_task["next_heartbeat_at"] is None


def test_task_update_rejects_payloads_without_editable_fields(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)

    created_task = client.post(
        "/tasks",
        json={"title": "Strict update task"},
    ).json()

    wrong_endpoint_response = client.patch(
        f"/tasks/{created_task['id']}",
        json={"status": "running"},
    )
    assert wrong_endpoint_response.status_code == 400
    assert wrong_endpoint_response.json()["detail"] == "task update requires at least one editable field"


def test_task_management_state_persists_across_app_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "task-persistence.db"
    first_client = TestClient(create_app(HivemindStore(db_path), start_scheduler=False))
    setup(first_client)
    first_user = first_client.get("/me").json()
    agent = first_client.get("/agents").json()[0]
    credential = first_client.get("/credentials").json()[0]

    create_response = first_client.post(
        "/tasks",
        json={
            "title": "Persist queued task",
            "description": "This task should survive a process restart.",
            "assigned_agent_id": agent["id"],
            "credential_id": credential["id"],
            "action": "read_repo",
            "intent": "Carry the task contract through a restart.",
            "heartbeat_seconds": 60,
        },
    )
    assert create_response.status_code == 201
    created_task = create_response.json()

    status_response = first_client.patch(f"/tasks/{created_task['id']}/status", json={"status": "blocked"})
    assert status_response.status_code == 200

    heartbeat_response = first_client.post(
        f"/tasks/{created_task['id']}/heartbeats",
        json={"note": "restart-safe heartbeat"},
    )
    assert heartbeat_response.status_code == 201
    recorded_heartbeat = heartbeat_response.json()
    first_client.close()

    second_client = TestClient(create_app(HivemindStore(db_path), start_scheduler=False))
    login_response = second_client.post(
        "/auth/login",
        json={"username": "admin", "password": TEST_PASSWORD},
    )
    assert login_response.status_code == 200

    tasks_response = second_client.get("/tasks")
    assert tasks_response.status_code == 200
    persisted_task = tasks_response.json()[0]
    assert persisted_task["id"] == created_task["id"]
    assert persisted_task["title"] == "Persist queued task"
    assert persisted_task["status"] == "blocked"
    assert persisted_task["assigned_agent_id"] == agent["id"]
    assert persisted_task["credential_id"] == credential["id"]
    assert persisted_task["action"] == "read_repo"
    assert persisted_task["intent"] == "Carry the task contract through a restart."
    assert persisted_task["heartbeat_seconds"] == 60
    assert persisted_task["next_heartbeat_at"] is not None

    heartbeats_response = second_client.get(f"/heartbeats?task_id={created_task['id']}")
    assert heartbeats_response.status_code == 200
    assert heartbeats_response.json() == [recorded_heartbeat]

    audit_response = second_client.get("/audit-events")
    assert audit_response.status_code == 200
    persisted_task_events = [event for event in audit_response.json() if event["target_id"] == created_task["id"]]
    assert [event["type"] for event in persisted_task_events] == [
        "task.heartbeat",
        "task.status.updated",
        "task.created",
    ]
    assert persisted_task_events[0]["actor_id"] == first_user["id"]
    assert persisted_task_events[0]["metadata"] == {"agent_id": agent["id"], "note": "restart-safe heartbeat"}
    assert persisted_task_events[1]["actor_id"] == first_user["id"]
    assert persisted_task_events[1]["reason"] == "task marked blocked"
    assert persisted_task_events[1]["metadata"] == {"from_status": "queued", "to_status": "blocked"}
    assert persisted_task_events[2]["actor_id"] == first_user["id"]
    assert persisted_task_events[2]["metadata"] == {
        "status": "queued",
        "priority": "normal",
        "assigned_agent_id": agent["id"],
        "credential_id": credential["id"],
        "action": "read_repo",
        "intent": "Carry the task contract through a restart.",
        "heartbeat_seconds": 60,
    }
    second_client.close()


def test_task_status_transitions_and_terminal_heartbeats_are_enforced(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)
    agent = client.get("/agents").json()[0]

    invalid_create = client.post(
        "/tasks",
        json={
            "title": "Invalid terminal start",
            "status": "done",
        },
    )
    assert invalid_create.status_code == 400
    assert invalid_create.json()["detail"] == "new tasks must start in one of: blocked, queued, running"

    task = client.post(
        "/tasks",
        json={
            "title": "Transition guard",
            "assigned_agent_id": agent["id"],
            "heartbeat_seconds": 60,
        },
    ).json()

    done_response = client.patch(f"/tasks/{task['id']}/status", json={"status": "done"})
    assert done_response.status_code == 200
    assert done_response.json()["status"] == "done"
    assert done_response.json()["next_heartbeat_at"] is None

    invalid_transition = client.patch(f"/tasks/{task['id']}/status", json={"status": "running"})
    assert invalid_transition.status_code == 400
    assert invalid_transition.json()["detail"] == "cannot transition task from done to running"

    terminal_heartbeat = client.post(
        f"/tasks/{task['id']}/heartbeats",
        json={"note": "should be rejected"},
    )
    assert terminal_heartbeat.status_code == 400
    assert terminal_heartbeat.json()["detail"] == "cannot record heartbeat for task in terminal status: done"

    blocked_task = client.post(
        "/tasks",
        json={
            "title": "Blocked transition guard",
            "status": "blocked",
            "assigned_agent_id": agent["id"],
        },
    ).json()
    blocked_to_queued = client.patch(f"/tasks/{blocked_task['id']}/status", json={"status": "queued"})
    assert blocked_to_queued.status_code == 200
    assert blocked_to_queued.json()["status"] == "queued"
    queued_to_running = client.patch(f"/tasks/{blocked_task['id']}/status", json={"status": "running"})
    assert queued_to_running.status_code == 200
    assert queued_to_running.json()["status"] == "running"

    failed_task = client.post(
        "/tasks",
        json={
            "title": "Failed transition guard",
            "assigned_agent_id": agent["id"],
            "heartbeat_seconds": 60,
        },
    ).json()
    failed_response = client.patch(f"/tasks/{failed_task['id']}/status", json={"status": "failed"})
    assert failed_response.status_code == 200
    assert failed_response.json()["status"] == "failed"
    assert failed_response.json()["next_heartbeat_at"] is None
    failed_heartbeat = client.post(
        f"/tasks/{failed_task['id']}/heartbeats",
        json={"note": "should be rejected"},
    )
    assert failed_heartbeat.status_code == 400
    assert failed_heartbeat.json()["detail"] == "cannot record heartbeat for task in terminal status: failed"

    cancelled_task = client.post(
        "/tasks",
        json={
            "title": "Cancelled transition guard",
            "assigned_agent_id": agent["id"],
            "heartbeat_seconds": 60,
        },
    ).json()
    running_response = client.patch(f"/tasks/{cancelled_task['id']}/status", json={"status": "running"})
    assert running_response.status_code == 200
    cancelled_response = client.patch(f"/tasks/{cancelled_task['id']}/status", json={"status": "cancelled"})
    assert cancelled_response.status_code == 200
    assert cancelled_response.json()["status"] == "cancelled"
    assert cancelled_response.json()["next_heartbeat_at"] is None
    cancelled_heartbeat = client.post(
        f"/tasks/{cancelled_task['id']}/heartbeats",
        json={"note": "should be rejected"},
    )
    assert cancelled_heartbeat.status_code == 400
    assert cancelled_heartbeat.json()["detail"] == "cannot record heartbeat for task in terminal status: cancelled"

    edited_done_task = client.patch(
        f"/tasks/{task['id']}",
        json={"heartbeat_seconds": 120},
    )
    assert edited_done_task.status_code == 200
    assert edited_done_task.json()["heartbeat_seconds"] == 120
    assert edited_done_task.json()["next_heartbeat_at"] is None


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


def test_legacy_schedule_priority_is_normalized_without_blocking_due_runs(tmp_path: Path) -> None:
    store = HivemindStore(tmp_path / "legacy-schedule-priority.db")
    client = TestClient(create_app(store, start_scheduler=False))
    setup(client)
    me = client.get("/me").json()
    agent = client.get("/agents").json()[0]

    legacy_schedule = client.post(
        "/schedules",
        json={
            "name": "Legacy priority schedule",
            "interval_seconds": 60,
            "task_title": "Legacy review task",
            "priority": "high",
            "assigned_agent_id": agent["id"],
            "next_run_at": "2000-01-01T00:00:00+00:00",
        },
    ).json()
    healthy_schedule = client.post(
        "/schedules",
        json={
            "name": "Healthy due schedule",
            "interval_seconds": 60,
            "task_title": "Healthy review task",
            "priority": "low",
            "assigned_agent_id": agent["id"],
            "next_run_at": "2000-01-01T00:00:00+00:00",
        },
    ).json()

    with store.connect() as conn:
        conn.execute(
            "UPDATE schedules SET priority = ?, updated_at = ? WHERE id = ?",
            ("legacy-urgent", "1999-01-01T00:00:00+00:00", legacy_schedule["id"]),
        )

    run_response = client.post("/schedules/run-due")

    assert run_response.status_code == 200
    created_tasks = {task["title"]: task for task in run_response.json()["created_tasks"]}
    assert set(created_tasks) == {"Legacy review task", "Healthy review task"}
    assert created_tasks["Legacy review task"]["priority"] == "normal"
    assert created_tasks["Healthy review task"]["priority"] == "low"

    schedules = {schedule["id"]: schedule for schedule in client.get("/schedules").json()}
    assert schedules[legacy_schedule["id"]]["priority"] == "normal"
    assert schedules[healthy_schedule["id"]]["priority"] == "low"

    normalized_event = next(
        event
        for event in client.get("/audit-events").json()
        if event["type"] == "schedule.priority.normalized" and event["target_id"] == legacy_schedule["id"]
    )
    assert normalized_event["actor_id"] == me["id"]
    assert normalized_event["decision"] == "allowed"
    assert normalized_event["reason"] == "legacy schedule priority normalized"
    assert normalized_event["metadata"] == {
        "from_priority": "legacy-urgent",
        "to_priority": "normal",
    }


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
