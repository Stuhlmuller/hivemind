from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import sqlite3
from threading import Barrier, Event
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

from hivemind.api import create_app
from hivemind.config import HivemindConfig, IntentReviewerConfig
from hivemind.oauth import SecretBox
from hivemind.policy import ProviderIntentReviewDecision, ProviderIntentReviewRequest, ProviderIntentReviewerError
from hivemind.providers import AgentProviderError, ProviderRunRequest, ProviderRunResult, ProviderToolRequest
from hivemind.store import HivemindStore, SCHEDULE_BACKFILL_BATCH_LIMIT, StoreError, hash_password

TEST_PASSWORD = "operator-not-secret"


class RecordingProviderReviewer:
    def __init__(self, *, allowed: bool = True, reason: str = "provider reviewer approved the request") -> None:
        self.allowed = allowed
        self.reason = reason
        self.requests: list[ProviderIntentReviewRequest] = []

    def review(self, request: ProviderIntentReviewRequest) -> ProviderIntentReviewDecision:
        self.requests.append(request)
        return ProviderIntentReviewDecision(allowed=self.allowed, reason=self.reason)


class FailingProviderReviewer:
    def review(self, request: ProviderIntentReviewRequest) -> ProviderIntentReviewDecision:
        raise ProviderIntentReviewerError(f"upstream failure for {request.reviewer_credential_ref}")


class RecordingAgentProviderAdapter:
    def __init__(self, provider: str = "openrouter") -> None:
        self.provider = provider
        self.requests: list[ProviderRunRequest] = []

    def provider_id(self) -> str:
        return self.provider

    def run(self, request: ProviderRunRequest) -> ProviderRunResult:
        self.requests.append(request)
        return ProviderRunResult(
            provider=request.provider,
            model=request.model,
            output_text=f"adapter response for {request.prompt}",
            tool_requests=tuple(request.tool_requests),
            metadata={"adapter": "recording"},
        )


class BlockingAgentProviderAdapter:
    def __init__(self, slow_task_id: str) -> None:
        self.slow_task_id = slow_task_id
        self.started = Barrier(2)
        self.release_slow = Event()

    def provider_id(self) -> str:
        return "openrouter"

    def run(self, request: ProviderRunRequest) -> ProviderRunResult:
        self.started.wait(timeout=5)
        if request.metadata["task_id"] == self.slow_task_id:
            self.release_slow.wait(timeout=5)
        return ProviderRunResult(
            provider=request.provider,
            model=request.model,
            output_text=f"adapter response for {request.prompt}",
        )


class LeakingAgentProviderAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def provider_id(self) -> str:
        return "openrouter"

    def run(self, request: ProviderRunRequest) -> ProviderRunResult:
        if self.fail:
            raise AgentProviderError("upstream rejected apiKey=raw-provider-token clientSecret=raw-provider-secret")
        return ProviderRunResult(
            provider=request.provider,
            model=request.model,
            output_text=f"provider used {request.credential_ref} with fallback env://SECONDARY_PROVIDER_SECRET",
            tool_requests=(
                ProviderToolRequest(
                    name="debug",
                    arguments={
                        "credential_ref": request.credential_ref,
                        "fallback_ref": "env://SECONDARY_PROVIDER_SECRET",
                        "notes": ["secondary ref env://SECONDARY_PROVIDER_SECRET"],
                        "token": "placeholder",
                        "accessToken": "LEAKME_TOKEN",
                        "apiKey": "raw-api-key",
                        "clientSecret": "LEAKME_SECRET",
                        "x-api-key": "test prefixed api key",
                        "authorization_header": "test authorization header",
                        "bearer_token": "test bearer token",
                    },
                ),
            ),
        )


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


def latest_schedule_run_event(client: TestClient, schedule_id: str) -> dict[str, object]:
    events = [
        event
        for event in client.get("/audit-events").json()
        if event["type"] == "schedule.ran" and event["target_id"] == schedule_id
    ]
    if not events:
        raise AssertionError(f"missing schedule.ran audit event for {schedule_id}")
    return events[0]


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
    for required in ['name="username"', 'name="password"', 'name="password_confirm"', 'autocomplete="new-password"']:
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
    require_true(
        "Create the first local operator account" in response.text,
        "frontend should describe first-run local account setup",
    )


def test_frontend_formats_structured_api_errors(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    response = client.get("/static/app.js")

    require_equal(response.status_code, 200, "frontend script should be served")
    require_true("function formatApiError" in response.text, "frontend should format API errors")
    require_true("function formatErrorItem" in response.text, "frontend should format validation items")
    require_true(
        "new Error(formatApiError(body, response.status))" in response.text,
        "API helper should use formatted error messages",
    )
    require_true("new Error(body.detail" not in response.text, "API helper should not stringify structured errors")


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


def test_setup_rejects_mismatched_password_confirmation(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    mismatch_response = client.post(
        "/auth/setup",
        json={
            "username": "admin",
            "password": TEST_PASSWORD,
            "password_confirm": "no",
        },
    )
    setup_state = client.get("/setup-state")
    setup_response = client.post(
        "/auth/setup",
        json={
            "username": "admin",
            "password": TEST_PASSWORD,
            "password_confirm": TEST_PASSWORD,
        },
    )

    require_equal(mismatch_response.status_code, 400, "setup should reject mismatched confirmation")
    require_equal(
        mismatch_response.json(),
        {"detail": "password confirmation does not match"},
        "setup should return a direct mismatch error",
    )
    require_equal(setup_state.json(), {"setup_complete": False}, "mismatch should not complete setup")
    require_equal(setup_response.status_code, 201, "matching confirmation should create the admin")
    require_equal(setup_response.json()["user"]["role"], "admin", "first user should be admin")


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


def test_config_exposes_redacted_provider_backed_reviewer_settings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_INTENT_REVIEWER_PROVIDER", "openrouter")
    monkeypatch.setenv("HIVEMIND_INTENT_REVIEWER_MODEL", "anthropic/claude-sonnet-4")
    monkeypatch.setenv("HIVEMIND_INTENT_REVIEWER_CREDENTIAL_REF", "env://OPENROUTER_API_KEY")
    client = client_for(tmp_path)

    setup(client)
    response = client.get("/config")
    reviewer = response.json()["intent_reviewer"]

    require_equal(response.status_code, 200, "config should return after setup")
    require_equal(reviewer["provider"], "openrouter", "config should expose the configured provider")
    require_equal(reviewer["model"], "anthropic/claude-sonnet-4", "config should expose the configured model")
    require_equal(reviewer["credential_ref_preview"], "env://OPE...", "config should redact the reviewer credential ref")
    require_true("OPENROUTER_API_KEY" not in response.text, "config should not expose the raw reviewer credential ref")


def test_config_rejects_invalid_reviewer_credential_ref(monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_INTENT_REVIEWER_CREDENTIAL_REF", "sk-raw-provider-secret")

    try:
        HivemindConfig.from_env()
    except ValueError as exc:
        require_equal(str(exc), "secret_ref must use env://, file://, vault://, oauth://, or secret://", "invalid reviewer refs should fail closed")
    else:
        raise AssertionError("invalid reviewer credential_ref was accepted")


def test_config_exposes_redacted_agent_provider_settings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_MODEL", "anthropic/claude-sonnet-4")
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_REF", "env://OPENROUTER_API_KEY")
    client = client_for(tmp_path)

    setup(client)
    response = client.get("/config")
    providers = {provider["provider"]: provider for provider in response.json()["agent_providers"]}
    expected_providers = {"openai", "codex", "claude", "gemini", "openrouter", "bedrock", "huggingface", "ollama"}

    require_equal(response.status_code, 200, "config should return after setup")
    require_true(expected_providers.issubset(providers), "config should list the supported remote agent providers")
    require_equal(providers["openrouter"]["model"], "anthropic/claude-sonnet-4", "provider config should expose the configured model")
    require_equal(providers["openrouter"]["credential_ref_preview"], "env://OPE...", "provider config should redact credential refs")
    require_true("credential_ref" not in providers["openrouter"], "provider config should not expose raw credential refs")
    require_true("OPENROUTER_API_KEY" not in response.text, "config should not expose raw provider credential refs")


def test_spawn_agent_uses_provider_config_model_when_model_is_omitted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_MODEL", "anthropic/claude-sonnet-4")
    client = client_for(tmp_path)
    setup(client)

    response = client.post(
        "/agents",
        json={
            "name": "Provider default runner",
            "role": "run tasks through configured model",
            "provider": "openrouter",
        },
    )

    require_equal(response.status_code, 201, "agent creation should allow omitted model")
    require_equal(response.json()["model"], "anthropic/claude-sonnet-4", "remote agents should use provider config model by default")


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

    replay_response = client.post(
        "/credential-actions",
        json={"lease_token": lease["lease_token"], "action": "read_repo", "payload": {"repo": "hivemind"}},
    )
    require_equal(replay_response.status_code, 403, "replayed lease use should be denied")
    require_equal(
        replay_response.json()["detail"],
        "credential lease is expired or revoked",
        "replayed lease use should expose the revoke/expiry reason",
    )

    stored_lease = client.get("/credential-leases").json()[0]
    require_equal(stored_lease["status"], "revoked", "successful broker use should consume the lease")
    require_true("lease_token" not in stored_lease, "public lease views must not expose the raw token")

    audit_events = client.get("/audit-events").json()
    credential_events = [event for event in audit_events if event["target_id"] == credential["id"]]
    require_equal(credential_events[0]["type"], "credential.action.denied", "replay denial should be audited")
    require_equal(credential_events[0]["decision"], "denied", "replay denial audit should be marked denied")
    require_equal(credential_events[1]["type"], "credential.action.performed", "successful broker use should be audited")
    require_equal(credential_events[1]["decision"], "allowed", "successful broker use audit should be marked allowed")


def test_provider_backed_reviewer_can_approve_store_backed_lease_requests(tmp_path: Path) -> None:
    reviewer = RecordingProviderReviewer(reason="openrouter reviewer approved request")
    store = HivemindStore(
        tmp_path / "hivemind.db",
        config=HivemindConfig(
            intent_reviewer=IntentReviewerConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4",
                credential_ref="env://OPENROUTER_API_KEY",
            )
        ),
        provider_reviewers={"openrouter": reviewer},
    )
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup(client)
    agent = client.get("/agents").json()[0]
    credential = client.get("/credentials").json()[0]

    response = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": agent["id"],
            "action": "read_repo",
            "intent": "Read repository metadata for safe task triage.",
            "ttl_seconds": 30,
        },
    )

    require_equal(response.status_code, 201, "provider-backed reviewer should allow a valid lease")
    require_true(bool(reviewer.requests), "provider-backed reviewer should receive the lease request")
    provider_request = reviewer.requests[0]
    require_equal(provider_request.reviewer_provider, "openrouter", "reviewer should receive the configured provider")
    require_equal(provider_request.reviewer_model, "anthropic/claude-sonnet-4", "reviewer should receive the configured model")
    require_equal(
        provider_request.reviewer_credential_ref,
        "env://OPENROUTER_API_KEY",
        "reviewer should receive the broker-owned credential reference",
    )
    require_equal(provider_request.credential_provider, "github", "reviewer should receive the leased credential provider")
    require_equal(provider_request.action, "read_repo", "reviewer should receive the normalized action")
    require_equal(response.json()["action"], "read_repo", "lease response should preserve the normalized action")


def test_unknown_provider_backed_reviewer_fails_closed_for_store_backed_lease_requests(tmp_path: Path) -> None:
    store = HivemindStore(
        tmp_path / "hivemind.db",
        config=HivemindConfig(
            intent_reviewer=IntentReviewerConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4",
                credential_ref="env://OPENROUTER_API_KEY",
            )
        ),
    )
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup(client)
    agent = client.get("/agents").json()[0]
    credential = client.get("/credentials").json()[0]

    response = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": agent["id"],
            "action": "read_repo",
            "intent": "Read repository metadata for safe task triage.",
            "ttl_seconds": 30,
        },
    )

    require_equal(response.status_code, 403, "unknown provider-backed reviewers should fail closed")
    require_true(
        "intent reviewer provider is not configured" in response.json()["detail"],
        "lease denial should explain that the provider adapter is missing",
    )


def test_provider_reviewer_errors_fail_closed_without_leaking_secret_refs(tmp_path: Path) -> None:
    store = HivemindStore(
        tmp_path / "hivemind.db",
        config=HivemindConfig(
            intent_reviewer=IntentReviewerConfig(
                provider="openrouter",
                model="anthropic/claude-sonnet-4",
                credential_ref="env://OPENROUTER_API_KEY",
            )
        ),
        provider_reviewers={"openrouter": FailingProviderReviewer()},
    )
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup(client)
    agent = client.get("/agents").json()[0]
    credential = client.get("/credentials").json()[0]

    response = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": agent["id"],
            "action": "read_repo",
            "intent": "Read repository metadata for safe task triage.",
            "ttl_seconds": 30,
        },
    )
    audit_events = client.get("/audit-events").json()

    require_equal(response.status_code, 403, "provider reviewer errors should deny leases")
    require_equal(response.json()["detail"], "intent reviewer provider failed closed", "denial should use a redacted reason")
    require_true("OPENROUTER_API_KEY" not in response.text, "denial should not expose reviewer credential refs")
    require_true("OPENROUTER_API_KEY" not in str(audit_events), "audit events should not expose reviewer credential refs")


def test_default_app_path_fails_closed_for_unregistered_provider_reviewer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_DB_PATH", str(tmp_path / "hivemind.db"))
    monkeypatch.setenv("HIVEMIND_INTENT_REVIEWER_PROVIDER", "openrouter")
    monkeypatch.setenv("HIVEMIND_INTENT_REVIEWER_MODEL", "anthropic/claude-sonnet-4")
    monkeypatch.setenv("HIVEMIND_INTENT_REVIEWER_CREDENTIAL_REF", "env://OPENROUTER_API_KEY")
    client = TestClient(create_app(start_scheduler=False), base_url="https://testserver")

    setup(client)
    agent = client.get("/agents").json()[0]
    credential = client.get("/credentials").json()[0]

    response = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": agent["id"],
            "action": "read_repo",
            "intent": "Read repository metadata for safe task triage.",
            "ttl_seconds": 30,
        },
    )

    require_equal(response.status_code, 403, "default app path should fail closed without a provider adapter")
    require_true(
        "intent reviewer provider is not configured" in response.json()["detail"],
        "default app path should not silently fall back to local review",
    )


def test_persisted_lease_concurrent_action_consumes_once(tmp_path: Path) -> None:
    db_path = tmp_path / "persisted-lease-race.db"
    setup_store = HivemindStore(db_path)
    setup_store.setup_admin("admin", TEST_PASSWORD)
    agent = setup_store.list_agents()[0]
    credential = setup_store.list_credentials()[0]
    lease_token, _ = setup_store.request_lease(
        credential_id=credential["id"],
        agent_id=agent["id"],
        action="read_repo",
        intent="Read repository metadata for safe task triage.",
        ttl_seconds=30,
    )
    if lease_token is None:
        raise AssertionError("active lease request should issue a token")

    start = Barrier(3)
    consume = Barrier(2)

    class RacingStore(HivemindStore):
        def _consume_credential_action(self, conn, lease, normalized_action, payload):
            consume.wait(timeout=5)
            return super()._consume_credential_action(conn, lease, normalized_action, payload)

    stores = [RacingStore(db_path), RacingStore(db_path)]

    def perform(store: HivemindStore) -> tuple[str, object]:
        start.wait(timeout=5)
        try:
            return (
                "ok",
                store.perform_credential_action(
                    lease_token=lease_token,
                    action="read_repo",
                    payload={"repo": "hivemind"},
                ),
            )
        except StoreError as exc:
            return ("error", str(exc))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(perform, store) for store in stores]
        start.wait(timeout=5)
        results = [future.result() for future in futures]

    require_equal(sum(1 for status, _ in results if status == "ok"), 1, "only one concurrent action should succeed")
    require_equal(
        [detail for status, detail in results if status == "error"],
        ["credential lease is expired or revoked"],
        "losing concurrent action should fail closed as a replay",
    )
    require_equal(setup_store.list_leases()[0]["status"], "revoked", "persisted lease should be consumed")

    action_events = [
        event for event in setup_store.list_audit_events() if event["type"].startswith("credential.action.")
    ]
    require_equal(
        sorted(event["type"] for event in action_events),
        ["credential.action.denied", "credential.action.performed"],
        "race should audit one allowed action and one denial",
    )


def test_local_agent_task_execution_uses_deterministic_adapter_without_network(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)
    agent = client.get("/agents").json()[0]
    task = client.post(
        "/tasks",
        json={
            "title": "Summarize runtime state",
            "description": "Summarize queued work without calling a remote provider.",
            "assigned_agent_id": agent["id"],
        },
    ).json()

    response = client.post(f"/tasks/{task['id']}/run", json={"input": "Use the local deterministic adapter."})
    tasks = client.get("/tasks").json()
    updated_task = next(item for item in tasks if item["id"] == task["id"])
    audit_events = client.get("/audit-events").json()

    require_equal(response.status_code, 201, "local task execution should succeed")
    result = response.json()
    require_equal(result["task_id"], task["id"], "task run should identify the executed task")
    require_equal(result["agent_id"], agent["id"], "task run should identify the executing agent")
    require_equal(result["provider"], "local", "local agent should use the deterministic local provider")
    require_equal(result["model"], agent["model"], "task run should use the agent-selected model")
    require_true("Use the local deterministic adapter." in result["output_text"], "local adapter should echo deterministic output")
    require_true("credential_ref" not in result, "task run response should not expose provider credential refs")
    require_equal(updated_task["status"], "done", "successful execution should mark the task done")
    require_true(
        any(event["type"] == "task.execution.completed" and event["target_id"] == task["id"] for event in audit_events),
        "task execution should be audited",
    )


def test_registered_agent_provider_adapter_receives_model_and_secret_ref_reference(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_MODEL", "anthropic/claude-sonnet-4")
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_REF", "env://OPENROUTER_API_KEY")
    adapter = RecordingAgentProviderAdapter("openrouter")
    store = HivemindStore(
        tmp_path / "hivemind.db",
        config=HivemindConfig.from_env(),
        agent_provider_adapters={"openrouter": adapter},
    )
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup(client)
    agent = client.post(
        "/agents",
        json={
            "name": "Provider runner",
            "role": "run a bounded provider-backed task",
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
            "system_prompt": "Keep output short.",
        },
    ).json()
    credential = client.post(
        "/credentials",
        json={
            "name": "Repo reader",
            "provider": "github",
            "secret_ref": "env://GITHUB_TOKEN",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["read_repo"],
        },
    ).json()
    task = client.post(
        "/tasks",
        json={
            "title": "Read repository metadata",
            "description": "Use the provider adapter boundary.",
            "assigned_agent_id": agent["id"],
            "credential_id": credential["id"],
            "action": "read_repo",
            "intent": "Read repository metadata for safe task triage.",
        },
    ).json()

    response = client.post(f"/tasks/{task['id']}/run", json={})

    require_equal(response.status_code, 201, "registered provider adapter should execute the task")
    require_true(bool(adapter.requests), "registered adapter should receive a provider run request")
    provider_request = adapter.requests[0]
    require_equal(provider_request.provider, "openrouter", "adapter request should use the agent provider")
    require_equal(provider_request.model, "anthropic/claude-sonnet-4", "adapter request should use the agent model")
    require_equal(provider_request.system_prompt, "Keep output short.", "adapter request should include the agent system prompt")
    require_equal(provider_request.credential_ref, "env://OPENROUTER_API_KEY", "adapter should receive only the provider credential reference")
    require_equal(provider_request.tool_requests[0].name, "read_repo", "adapter should receive task tool requests")
    require_equal(
        provider_request.tool_requests[0].arguments["credential_id"],
        credential["id"],
        "tool request should reference credentials by id",
    )
    require_true("OPENROUTER_API_KEY" not in response.text, "task run response should not expose raw provider credential refs")
    require_true("credential_ref" not in response.json(), "task run response should omit provider credential refs")


def test_agent_provider_task_credential_policy_denial_fails_before_adapter(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_REF", "env://OPENROUTER_API_KEY")
    adapter = RecordingAgentProviderAdapter("openrouter")
    store = HivemindStore(
        tmp_path / "hivemind.db",
        config=HivemindConfig.from_env(),
        agent_provider_adapters={"openrouter": adapter},
    )
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup(client)
    allowed_agent = client.get("/agents").json()[0]
    denied_agent = client.post(
        "/agents",
        json={
            "name": "Denied provider runner",
            "role": "should not receive credential-scoped tool requests",
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
        },
    ).json()
    credential = client.post(
        "/credentials",
        json={
            "name": "Repo reader",
            "provider": "github",
            "secret_ref": "env://GITHUB_TOKEN",
            "allowed_agents": [allowed_agent["id"]],
            "allowed_actions": ["read_repo"],
        },
    ).json()
    task = client.post(
        "/tasks",
        json={
            "title": "Denied repository metadata read",
            "description": "The assigned agent is outside the credential policy.",
            "assigned_agent_id": denied_agent["id"],
            "credential_id": credential["id"],
            "action": "read_repo",
            "intent": "Read repository metadata for safe task triage.",
        },
    ).json()

    response = client.post(f"/tasks/{task['id']}/run", json={})
    updated_task = next(item for item in client.get("/tasks").json() if item["id"] == task["id"])
    audit_events = client.get("/audit-events").json()

    require_equal(response.status_code, 403, "credential policy denial should fail closed before provider execution")
    require_equal(response.json()["detail"], "agent is not allowed to use this credential", "denial reason should match policy")
    require_equal(adapter.requests, [], "provider adapter should not receive policy-denied credential tool requests")
    require_equal(updated_task["status"], "failed", "policy-denied provider task should be marked failed")
    require_true(
        any(
            event["type"] == "credential.lease.denied"
            and event["target_id"] == credential["id"]
            and event["metadata"]["task_id"] == task["id"]
            for event in audit_events
        ),
        "policy-denied provider tool requests should be audited",
    )


def test_agent_provider_task_approval_required_action_fails_before_adapter(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_REF", "env://OPENROUTER_API_KEY")
    adapter = RecordingAgentProviderAdapter("openrouter")
    store = HivemindStore(
        tmp_path / "hivemind.db",
        config=HivemindConfig.from_env(),
        agent_provider_adapters={"openrouter": adapter},
    )
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup(client)
    agent = client.post(
        "/agents",
        json={
            "name": "Approval gated provider runner",
            "role": "should not bypass operator approval",
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
        },
    ).json()
    credential = client.post(
        "/credentials",
        json={
            "name": "Repo reader",
            "provider": "github",
            "secret_ref": "env://GITHUB_TOKEN",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["read_repo"],
            "approval_required_actions": ["read_repo"],
        },
    ).json()
    task = client.post(
        "/tasks",
        json={
            "title": "Approval-gated repository metadata read",
            "description": "The credential action requires operator approval.",
            "assigned_agent_id": agent["id"],
            "credential_id": credential["id"],
            "action": "read_repo",
            "intent": "Read repository metadata for safe task triage.",
        },
    ).json()

    response = client.post(f"/tasks/{task['id']}/run", json={})
    updated_task = next(item for item in client.get("/tasks").json() if item["id"] == task["id"])

    require_equal(response.status_code, 403, "approval-gated actions should fail closed before provider execution")
    require_equal(
        response.json()["detail"],
        "credential action requires operator-approved lease",
        "approval-gated task runs should explain that a lease is required",
    )
    require_equal(adapter.requests, [], "provider adapter should not receive approval-gated credential tool requests")
    require_equal(updated_task["status"], "failed", "approval-gated provider task should be marked failed")


def test_concurrent_agent_task_execution_claims_queued_task_once(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_REF", "env://OPENROUTER_API_KEY")
    db_path = tmp_path / "task-run-race.db"
    adapter = RecordingAgentProviderAdapter("openrouter")
    setup_store = HivemindStore(
        db_path,
        config=HivemindConfig.from_env(),
        agent_provider_adapters={"openrouter": adapter},
    )
    agent = setup_store.create_agent(
        {
            "name": "Provider runner",
            "role": "run one queued task",
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
        }
    )
    task = setup_store.create_task(
        {
            "title": "Race provider execution",
            "description": "Only one caller may claim this task.",
            "assigned_agent_id": agent["id"],
        }
    )
    read_task = Barrier(2)

    class RacingStore(HivemindStore):
        def get_task_row(self, conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
            row = super().get_task_row(conn, task_id)
            if task_id == task["id"] and row["status"] == "queued":
                read_task.wait(timeout=5)
            return row

    stores = [
        RacingStore(db_path, config=HivemindConfig.from_env(), agent_provider_adapters={"openrouter": adapter}),
        RacingStore(db_path, config=HivemindConfig.from_env(), agent_provider_adapters={"openrouter": adapter}),
    ]

    def run(store: HivemindStore) -> tuple[str, object]:
        try:
            return ("ok", store.run_task(task["id"]))
        except StoreError as exc:
            return ("error", str(exc))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result() for future in [executor.submit(run, store) for store in stores]]

    require_equal(sum(1 for status, _ in results if status == "ok"), 1, "only one task run should claim execution")
    require_equal(
        [detail for status, detail in results if status == "error"],
        ["only queued tasks can be executed"],
        "losing task run should fail before provider execution",
    )
    require_equal(len(adapter.requests), 1, "provider adapter should execute the claimed task exactly once")
    require_equal(setup_store.get_task(task["id"])["status"], "done", "claimed task should finish successfully")


def test_agent_stays_working_until_same_agent_running_tasks_finish(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_REF", "env://OPENROUTER_API_KEY")
    db_path = tmp_path / "same-agent-running-tasks.db"
    setup_store = HivemindStore(db_path, config=HivemindConfig.from_env())
    agent = setup_store.create_agent(
        {
            "name": "Provider runner",
            "role": "run multiple queued tasks",
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
        }
    )
    fast_task = setup_store.create_task(
        {
            "title": "Fast provider execution",
            "description": "This task finishes while another task is still running.",
            "assigned_agent_id": agent["id"],
        }
    )
    slow_task = setup_store.create_task(
        {
            "title": "Slow provider execution",
            "description": "This task remains running until the first task completes.",
            "assigned_agent_id": agent["id"],
        }
    )
    adapter = BlockingAgentProviderAdapter(slow_task["id"])
    stores = [
        HivemindStore(db_path, config=HivemindConfig.from_env(), agent_provider_adapters={"openrouter": adapter}),
        HivemindStore(db_path, config=HivemindConfig.from_env(), agent_provider_adapters={"openrouter": adapter}),
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        fast_result = executor.submit(stores[0].run_task, fast_task["id"])
        slow_result = executor.submit(stores[1].run_task, slow_task["id"])
        try:
            require_equal(
                fast_result.result(timeout=5)["task_id"],
                fast_task["id"],
                "fast task should finish first",
            )
            require_equal(
                setup_store.get_task(slow_task["id"])["status"],
                "running",
                "slow task should remain running",
            )
            require_equal(
                setup_store.get_agent(agent["id"])["status"],
                "working",
                "agent should stay working while another assigned task is running",
            )
        finally:
            adapter.release_slow.set()
        require_equal(
            slow_result.result(timeout=5)["task_id"],
            slow_task["id"],
            "slow task should finish after release",
        )

    require_equal(
        setup_store.get_agent(agent["id"])["status"],
        "idle",
        "agent should become idle after all runs finish",
    )


def test_remote_agent_provider_requires_credential_ref_before_adapter_execution(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_REF", raising=False)
    adapter = RecordingAgentProviderAdapter("openrouter")
    store = HivemindStore(
        tmp_path / "hivemind.db",
        config=HivemindConfig.from_env(),
        agent_provider_adapters={"openrouter": adapter},
    )
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup(client)
    agent = client.post(
        "/agents",
        json={
            "name": "Missing credential runner",
            "role": "exercise provider credential fail-closed behavior",
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
        },
    ).json()
    task = client.post(
        "/tasks",
        json={
            "title": "Provider task",
            "description": "This should fail before adapter execution.",
            "assigned_agent_id": agent["id"],
        },
    ).json()

    response = client.post(f"/tasks/{task['id']}/run", json={})
    updated_task = next(item for item in client.get("/tasks").json() if item["id"] == task["id"])

    require_equal(response.status_code, 403, "remote providers without credential refs should fail closed")
    require_true(
        "agent provider credential_ref is not configured" in response.json()["detail"],
        "failure should explain that provider credentials are missing",
    )
    require_equal(adapter.requests, [], "provider adapter should not run without a configured credential ref")
    require_equal(updated_task["status"], "failed", "credential configuration failures should mark the task failed")


def test_unregistered_agent_provider_fails_closed_without_leaking_secret_ref(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_REF", "env://OPENROUTER_API_KEY")
    store = HivemindStore(tmp_path / "hivemind.db", config=HivemindConfig.from_env())
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup(client)
    agent = client.post(
        "/agents",
        json={
            "name": "Missing adapter runner",
            "role": "exercise fail-closed provider execution",
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
        },
    ).json()
    task = client.post(
        "/tasks",
        json={
            "title": "Provider task",
            "description": "This should fail closed without an adapter.",
            "assigned_agent_id": agent["id"],
        },
    ).json()

    response = client.post(f"/tasks/{task['id']}/run", json={})
    audit_events = client.get("/audit-events").json()
    updated_task = next(item for item in client.get("/tasks").json() if item["id"] == task["id"])

    require_equal(response.status_code, 403, "unregistered remote providers should fail closed")
    require_true(
        "agent provider adapter is not configured" in response.json()["detail"],
        "failure should explain that the provider adapter is missing",
    )
    require_true("OPENROUTER_API_KEY" not in response.text, "failure should not expose the provider credential ref")
    require_true("OPENROUTER_API_KEY" not in str(audit_events), "audit should not expose provider credential refs")
    require_equal(updated_task["status"], "failed", "failed provider execution should mark the task failed")


def test_agent_provider_results_redact_secret_refs_from_public_response(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_REF", "env://OPENROUTER_API_KEY")
    store = HivemindStore(
        tmp_path / "hivemind.db",
        config=HivemindConfig.from_env(),
        agent_provider_adapters={"openrouter": LeakingAgentProviderAdapter()},
    )
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup(client)
    agent = client.post(
        "/agents",
        json={
            "name": "Leaky adapter runner",
            "role": "exercise provider redaction",
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
        },
    ).json()
    task = client.post(
        "/tasks",
        json={
            "title": "Provider redaction task",
            "description": "The adapter response includes configured refs.",
            "assigned_agent_id": agent["id"],
        },
    ).json()

    response = client.post(f"/tasks/{task['id']}/run", json={})
    result = response.json()

    require_equal(response.status_code, 201, "provider result redaction should still allow successful task runs")
    require_true("OPENROUTER_API_KEY" not in response.text, "provider result should not expose credential ref targets")
    require_true("env://OPENROUTER_API_KEY" not in response.text, "provider result should not expose full credential refs")
    require_true("SECONDARY_PROVIDER_SECRET" not in response.text, "provider result should not expose other secret ref targets")
    require_true("env://SEC..." in response.text, "provider result should preview other secret refs")
    require_equal(result["tool_requests"][0]["arguments"]["credential_ref"], "[redacted]", "credential_ref arguments should be redacted")
    require_equal(
        result["tool_requests"][0]["arguments"]["fallback_ref"],
        "env://SEC...",
        "non-sensitive secret-ref arguments should be previewed",
    )
    require_equal(
        result["tool_requests"][0]["arguments"]["notes"],
        ["secondary ref env://SEC..."],
        "secret refs embedded in free-text provider output should be previewed",
    )
    require_equal(result["tool_requests"][0]["arguments"]["token"], "[redacted]", "token-like arguments should be redacted")
    require_equal(result["tool_requests"][0]["arguments"]["accessToken"], "[redacted]", "camelCase token fields should be redacted")
    require_equal(result["tool_requests"][0]["arguments"]["apiKey"], "[redacted]", "camelCase key fields should be redacted")
    require_equal(result["tool_requests"][0]["arguments"]["clientSecret"], "[redacted]", "camelCase secret fields should be redacted")
    require_equal(
        result["tool_requests"][0]["arguments"]["x-api-key"],
        "[redacted]",
        "prefixed API key fields should be redacted",
    )
    require_equal(
        result["tool_requests"][0]["arguments"]["authorization_header"],
        "[redacted]",
        "authorization header fields should be redacted",
    )
    require_equal(
        result["tool_requests"][0]["arguments"]["bearer_token"],
        "[redacted]",
        "bearer token fields should be redacted",
    )


def test_agent_provider_error_messages_redact_secret_refs_from_response_and_audit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_REF", "env://OPENROUTER_API_KEY")
    store = HivemindStore(
        tmp_path / "hivemind.db",
        config=HivemindConfig.from_env(),
        agent_provider_adapters={"openrouter": LeakingAgentProviderAdapter(fail=True)},
    )
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup(client)
    agent = client.post(
        "/agents",
        json={
            "name": "Failing adapter runner",
            "role": "exercise provider error redaction",
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
        },
    ).json()
    task = client.post(
        "/tasks",
        json={
            "title": "Provider error task",
            "description": "The adapter error includes configured refs.",
            "assigned_agent_id": agent["id"],
        },
    ).json()

    response = client.post(f"/tasks/{task['id']}/run", json={})
    audit_events = client.get("/audit-events").json()

    require_equal(response.status_code, 403, "provider errors should fail closed")
    require_equal(response.json()["detail"], "agent provider failed closed", "provider error details should be sanitized")
    require_true("raw-provider-token" not in response.text, "provider error response should not expose adapter token details")
    require_true("raw-provider-secret" not in str(audit_events), "provider error audit should not expose adapter secret details")
    require_true("OPENROUTER_API_KEY" not in response.text, "provider error response should not expose credential refs")
    require_true("OPENROUTER_API_KEY" not in str(audit_events), "provider error audit should not expose credential refs")


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


def test_public_credential_metadata_redacts_secret_like_values(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)
    agent = client.get("/agents").json()[0]

    response = client.post(
        "/credentials",
        json={
            "name": "Provider Metadata",
            "provider": "openrouter",
            "secret_ref": "env://OPENROUTER_API_KEY",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["read_repo"],
            "metadata": {
                "credential_kind": "generic_reference",
                "credential_ref": "env://OPENROUTER_API_KEY",
                "fallback_ref": "env://SECONDARY_PROVIDER_SECRET",
                "oauth_token_expires_at": "2026-05-17T19:00:00+00:00",
                "nested": {"apiKey": "LEAKME_KEY", "accessToken": "LEAKME_ACCESS_TOKEN"},
            },
        },
    )

    credential = response.json()

    require_equal(response.status_code, 201, "credential metadata with secret-like values should be accepted")
    require_equal(credential["metadata"]["credential_ref"], "[redacted]", "metadata credential refs should be redacted by key")
    require_equal(credential["metadata"]["fallback_ref"], "env://SEC...", "secret refs in metadata values should be previewed")
    require_equal(
        credential["metadata"]["oauth_token_expires_at"],
        "2026-05-17T19:00:00+00:00",
        "OAuth expiry metadata should remain visible because it is not token material",
    )
    require_equal(credential["metadata"]["nested"]["apiKey"], "[redacted]", "nested secret-like metadata keys should be redacted")
    require_equal(credential["metadata"]["nested"]["accessToken"], "[redacted]", "nested token metadata keys should be redacted")
    require_true("OPENROUTER_API_KEY" not in response.text, "credential metadata should not expose the primary secret ref target")
    require_true("SECONDARY_PROVIDER_SECRET" not in response.text, "credential metadata should not expose secondary secret ref targets")
    require_true("LEAKME_KEY" not in response.text, "credential metadata should not expose token-like metadata values")
    require_true("LEAKME_ACCESS_TOKEN" not in response.text, "credential metadata should not expose token-like metadata values")


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
        ("POST", "/tasks/task_demo/run", {"input": "run this task"}),
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
    assert response.json()["detail"] == "secret_ref must use env://, file://, vault://, oauth://, or secret://"


def test_create_credential_rejects_client_supplied_secret_ref(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)

    response = client.post(
        "/credentials",
        json={
            "name": "Forged Broker Secret",
            "provider": "openrouter",
            "secret_ref": "secret://cred_existing",
            "allowed_actions": ["review_intent"],
        },
    )

    require_equal(response.status_code, 400, "client-supplied secret:// refs should be rejected")
    require_equal(
        response.json()["detail"],
        "secret:// refs are broker-generated; provide secret_value for broker-managed storage",
        "client-supplied secret:// refs should explain how to use managed storage",
    )


def test_store_rejects_client_supplied_broker_secret_ref(tmp_path: Path) -> None:
    store = HivemindStore(tmp_path / "hivemind.db")

    try:
        store.create_credential(
            {
                "name": "Forged Broker Secret",
                "provider": "openrouter",
                "secret_ref": "secret://cred_existing",
                "allowed_actions": ["review_intent"],
            }
        )
    except StoreError as exc:
        require_equal(
            str(exc),
            "secret:// refs are broker-generated; provide secret_value for broker-managed storage",
            "store should preserve broker-managed secret_ref invariant",
        )
    else:
        raise AssertionError("client-supplied secret:// credential was accepted")


def test_create_credential_rejects_managed_secret_kind_for_external_ref(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)

    response = client.post(
        "/credentials",
        json={
            "name": "Forged Managed Secret",
            "provider": "openrouter",
            "secret_ref": "env://OPENROUTER_API_KEY",
            "allowed_actions": ["review_intent"],
            "metadata": {"credential_kind": "managed_secret"},
        },
    )

    require_equal(response.status_code, 400, "external refs should not claim broker-managed secret metadata")
    require_equal(
        response.json()["detail"],
        "managed_secret metadata is broker-generated; provide secret_value for broker-managed storage",
        "external refs should explain how to use managed storage",
    )


def test_store_rejects_managed_secret_kind_for_external_ref(tmp_path: Path) -> None:
    store = HivemindStore(tmp_path / "hivemind.db")

    try:
        store.create_credential(
            {
                "name": "Forged Managed Secret",
                "provider": "openrouter",
                "secret_ref": "env://OPENROUTER_API_KEY",
                "allowed_actions": ["review_intent"],
                "metadata": {"credential_kind": "managed_secret"},
            }
        )
    except StoreError as exc:
        require_equal(
            str(exc),
            "managed_secret metadata is broker-generated; provide secret_value for broker-managed storage",
            "store should preserve broker-managed metadata invariant",
        )
    else:
        raise AssertionError("external ref with managed_secret metadata was accepted")


def test_broker_managed_secret_requires_secret_store_key(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)

    response = client.post(
        "/credentials",
        json={
            "name": "Broker Secret",
            "provider": "openrouter",
            "secret_value": "sk-test-local-secret",
            "allowed_actions": ["review_intent"],
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Set HIVEMIND_SECRETS_KEY to enable broker-side local secret storage."
    assert "sk-test-local-secret" not in response.text


def test_broker_managed_secret_is_encrypted_redacted_and_broker_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HIVEMIND_SECRETS_KEY", "local-test-secret-key")
    client = client_for(tmp_path)
    setup(client)
    agent = client.get("/agents").json()[0]
    managed_value = "  -----BEGIN TEST SECRET-----\nline-one\nline-two\n-----END TEST SECRET-----\n"

    response = client.post(
        "/credentials",
        json={
            "name": "Broker Secret",
            "provider": "openrouter",
            "secret_value": managed_value,
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["review_intent"],
            "max_ttl_seconds": 180,
            "require_intent": True,
            "metadata": {"credential_kind": "generic_reference", "note": "operator supplied"},
        },
    )

    assert response.status_code == 201
    credential = response.json()
    assert credential["provider"] == "openrouter"
    assert credential["metadata"]["credential_kind"] == "managed_secret"
    require_equal(credential["metadata"]["note"], "operator supplied", "managed secrets should preserve non-kind metadata")
    assert credential["secret_ref_preview"].startswith("secret://")
    assert "secret_value" not in credential
    assert managed_value not in response.text

    list_response = client.get("/credentials")
    assert list_response.status_code == 200
    assert managed_value not in list_response.text

    store = client.app.state.store
    secret_box = SecretBox.from_env()
    assert secret_box is not None
    assert store.resolve_broker_secret(credential["id"], secret_box) == managed_value

    conn = sqlite3.connect(tmp_path / "hivemind.db")
    try:
        row = conn.execute(
            "SELECT secret_ref FROM credentials WHERE id = ?",
            (credential["id"],),
        ).fetchone()
        secret_row = conn.execute(
            "SELECT ciphertext FROM broker_secrets WHERE credential_id = ?",
            (credential["id"],),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] == f"secret://{credential['id']}"
    assert secret_row is not None
    assert "BEGIN TEST SECRET" not in secret_row[0]


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


def test_tasks_heartbeats_and_due_schedules_run_once_by_default(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)
    agent = client.get("/agents").json()[0]
    base_now = datetime.now(timezone.utc).replace(microsecond=0)

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
            "next_run_at": (base_now - timedelta(seconds=190)).isoformat(),
        },
    )
    require_equal(schedule_response.status_code, 201, "schedule creation should succeed")
    schedule = schedule_response.json()
    require_equal(schedule["catch_up_policy"], "run_once", "schedules should default to run_once")
    require_true(schedule["enabled"] is True, "schedules should start enabled")

    disable_response = client.patch(f"/schedules/{schedule['id']}", json={"enabled": False})
    require_equal(disable_response.status_code, 200, "schedule pause should succeed")
    require_equal(disable_response.json()["id"], schedule["id"], "pause should target the requested schedule")
    require_true(disable_response.json()["enabled"] is False, "pause should disable the schedule")

    paused_run_response = client.post("/schedules/run-due")
    require_equal(paused_run_response.status_code, 200, "running due schedules while paused should succeed")
    require_equal(paused_run_response.json()["created_tasks"], [], "paused schedules should not create tasks")

    resumed_schedule = client.patch(f"/schedules/{schedule['id']}", json={"enabled": True})
    require_equal(resumed_schedule.status_code, 200, "schedule resume should succeed")
    require_equal(resumed_schedule.json()["id"], schedule["id"], "resume should target the requested schedule")
    require_true(resumed_schedule.json()["enabled"] is True, "resume should re-enable the schedule")

    run_response = client.post("/schedules/run-due")
    require_equal(run_response.status_code, 200, "running due schedules should succeed")
    created_tasks = run_response.json()["created_tasks"]
    require_equal(len(created_tasks), 1, "run_once should create exactly one task")
    require_equal(created_tasks[0]["title"], "Scheduled policy review", "run_once should create the scheduled task template")

    second_run_response = client.post("/schedules/run-due")
    require_equal(second_run_response.status_code, 200, "a second immediate due run check should succeed")
    require_equal(second_run_response.json()["created_tasks"], [], "run_once should advance the schedule after one execution")

    updated_schedule = next(item for item in client.get("/schedules").json() if item["id"] == schedule["id"])
    require_true(updated_schedule["enabled"] is True, "the schedule should remain enabled after resuming")
    require_true(updated_schedule["last_run_at"] is not None, "running the schedule should record last_run_at")
    last_run_at = datetime.fromisoformat(updated_schedule["last_run_at"])
    next_run_at = datetime.fromisoformat(updated_schedule["next_run_at"])
    require_equal(
        next_run_at - last_run_at,
        timedelta(seconds=60),
        "run_once should reset cadence from the current execution time",
    )
    metadata = latest_schedule_run_event(client, schedule["id"])["metadata"]
    require_equal(metadata["catch_up_policy"], "run_once", "audit metadata should record the active catch-up policy")
    require_equal(metadata["created_task_count"], 1, "run_once audit metadata should report one created task")
    require_true(metadata["missed_run_count"] >= 4, "run_once should record the number of overdue schedule slots")
    require_equal(
        metadata["skipped_run_count"],
        metadata["missed_run_count"] - 1,
        "run_once should report every skipped missed run after the immediate catch-up task",
    )
    require_equal(metadata["task_ids"], [created_tasks[0]["id"]], "run_once should audit the created task id")
    require_equal(len(metadata["scheduled_for"]), 1, "run_once should audit the single executed slot")
    schedule_run_event = latest_schedule_run_event(client, schedule["id"])
    require_equal(schedule_run_event["actor_id"], agent["id"], "schedule audit should attribute the assigned agent")
    require_equal(schedule_run_event["target_id"], schedule["id"], "schedule audit should target the schedule id")
    require_equal(schedule_run_event["decision"], "allowed", "schedule audit should record an allowed decision")
    require_equal(schedule_run_event["reason"], "scheduled task created", "schedule audit should describe the created task")


def test_due_schedules_skip_missed_runs_and_preserve_cadence(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)
    base_now = datetime.now(timezone.utc).replace(microsecond=0)

    schedule_response = client.post(
        "/schedules",
        json={
            "name": "Cadence-preserving review",
            "interval_seconds": 60,
            "catch_up_policy": "skip_missed",
            "task_title": "Cadence-preserving scheduled review",
            "next_run_at": (base_now - timedelta(seconds=190)).isoformat(),
        },
    )
    require_equal(schedule_response.status_code, 201, "schedule creation should succeed")
    schedule = schedule_response.json()

    run_response = client.post("/schedules/run-due")
    require_equal(run_response.status_code, 200, "running due schedules should succeed")
    require_equal(len(run_response.json()["created_tasks"]), 1, "skip_missed should create one current task")

    updated_schedule = next(item for item in client.get("/schedules").json() if item["id"] == schedule["id"])
    last_run_at = datetime.fromisoformat(updated_schedule["last_run_at"])
    next_run_at = datetime.fromisoformat(updated_schedule["next_run_at"])
    require_true(
        timedelta(0) < next_run_at - last_run_at < timedelta(seconds=60),
        "skip_missed should preserve the existing cadence instead of drifting from the current execution time",
    )
    metadata = latest_schedule_run_event(client, schedule["id"])["metadata"]
    require_equal(metadata["catch_up_policy"], "skip_missed", "audit metadata should record skip_missed")
    require_equal(metadata["created_task_count"], 1, "skip_missed should create one task for the latest due slot")
    require_equal(
        metadata["skipped_run_count"],
        metadata["missed_run_count"] - 1,
        "skip_missed should report the older missed slots it intentionally discarded",
    )
    require_equal(len(metadata["scheduled_for"]), 1, "skip_missed should audit one scheduled run slot")


def test_due_schedules_backfill_every_missed_run(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)
    base_now = datetime.now(timezone.utc).replace(microsecond=0)

    schedule_response = client.post(
        "/schedules",
        json={
            "name": "Backfill review",
            "interval_seconds": 60,
            "catch_up_policy": "backfill",
            "task_title": "Backfill scheduled review",
            "next_run_at": (base_now - timedelta(seconds=190)).isoformat(),
        },
    )
    require_equal(schedule_response.status_code, 201, "schedule creation should succeed")
    schedule = schedule_response.json()

    run_response = client.post("/schedules/run-due")
    require_equal(run_response.status_code, 200, "running due schedules should succeed")
    require_equal(len(run_response.json()["created_tasks"]), 4, "backfill should create one task per missed run")

    updated_schedule = next(item for item in client.get("/schedules").json() if item["id"] == schedule["id"])
    last_run_at = datetime.fromisoformat(updated_schedule["last_run_at"])
    next_run_at = datetime.fromisoformat(updated_schedule["next_run_at"])
    require_true(
        timedelta(0) < next_run_at - last_run_at < timedelta(seconds=60),
        "backfill should resume on the next scheduled slot after catching up",
    )
    metadata = latest_schedule_run_event(client, schedule["id"])["metadata"]
    require_equal(metadata["catch_up_policy"], "backfill", "audit metadata should record backfill")
    require_equal(metadata["created_task_count"], 4, "backfill should report every created catch-up task")
    require_equal(metadata["missed_run_count"], 4, "backfill should report the number of overdue slots")
    require_equal(metadata["skipped_run_count"], 0, "backfill should not skip any missed runs")
    require_equal(len(metadata["task_ids"]), 4, "backfill should audit each created task id")
    require_equal(len(metadata["scheduled_for"]), 4, "backfill should audit each scheduled slot it replayed")


def test_due_schedules_run_once_counts_long_downtime_without_expanding_slots(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)

    schedule_response = client.post(
        "/schedules",
        json={
            "name": "Long downtime review",
            "interval_seconds": 60,
            "catch_up_policy": "run_once",
            "task_title": "Long downtime scheduled review",
            "next_run_at": "2000-01-01T00:00:00+00:00",
        },
    )
    require_equal(schedule_response.status_code, 201, "schedule creation should succeed")
    schedule = schedule_response.json()

    run_response = client.post("/schedules/run-due")
    require_equal(run_response.status_code, 200, "long-overdue run_once schedules should not hang")
    require_equal(len(run_response.json()["created_tasks"]), 1, "run_once should still create exactly one task")

    metadata = latest_schedule_run_event(client, schedule["id"])["metadata"]
    require_true(metadata["missed_run_count"] > 1_000_000, "long downtime should be counted arithmetically")
    require_equal(metadata["created_task_count"], 1, "run_once should not create one task per missed slot")
    require_equal(
        metadata["skipped_run_count"],
        metadata["missed_run_count"] - 1,
        "run_once should report skipped downtime slots without materializing tasks",
    )
    require_equal(metadata["remaining_run_count"], 0, "run_once should not leave catch-up work queued")
    require_equal(len(metadata["scheduled_for"]), 1, "run_once should audit only the immediate recovery run")


def test_due_schedules_backfill_batches_long_downtime(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)
    base_now = datetime.now(timezone.utc).replace(microsecond=0)

    schedule_response = client.post(
        "/schedules",
        json={
            "name": "Batched backfill review",
            "interval_seconds": 60,
            "catch_up_policy": "backfill",
            "task_title": "Batched backfill scheduled review",
            "next_run_at": (base_now - timedelta(seconds=60 * (SCHEDULE_BACKFILL_BATCH_LIMIT + 10))).isoformat(),
        },
    )
    require_equal(schedule_response.status_code, 201, "schedule creation should succeed")
    schedule = schedule_response.json()

    run_response = client.post("/schedules/run-due")
    require_equal(run_response.status_code, 200, "long-overdue backfill schedules should not hang")
    require_equal(
        len(run_response.json()["created_tasks"]),
        SCHEDULE_BACKFILL_BATCH_LIMIT,
        "backfill should create only a bounded batch in one scheduler pass",
    )

    updated_schedule = next(item for item in client.get("/schedules").json() if item["id"] == schedule["id"])
    require_true(
        datetime.fromisoformat(updated_schedule["next_run_at"]) <= datetime.now(timezone.utc),
        "partial backfill should leave the next unprocessed slot due for a later scheduler pass",
    )
    metadata = latest_schedule_run_event(client, schedule["id"])["metadata"]
    require_equal(
        metadata["created_task_count"],
        SCHEDULE_BACKFILL_BATCH_LIMIT,
        "backfill audit should record the bounded batch size",
    )
    require_true(metadata["remaining_run_count"] > 0, "backfill audit should report unprocessed missed slots")
    require_equal(metadata["skipped_run_count"], 0, "backfill should defer rather than skip excess missed slots")


def test_due_schedules_rejects_malformed_existing_next_run_at(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)

    schedule_response = client.post(
        "/schedules",
        json={
            "name": "Malformed persisted schedule",
            "interval_seconds": 60,
            "task_title": "Scheduled task",
            "next_run_at": "2000-01-01T00:00:00+00:00",
        },
    )
    require_equal(schedule_response.status_code, 201, "schedule creation should succeed")
    schedule = schedule_response.json()

    with sqlite3.connect(tmp_path / "hivemind.db") as conn:
        conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", ("0-not-a-date", schedule["id"]))

    run_response = client.post("/schedules/run-due")

    require_equal(run_response.status_code, 400, "malformed persisted schedule timestamps should return a clean 4xx")
    require_equal(
        run_response.json()["detail"],
        "schedule next_run_at must be a valid ISO datetime",
        "malformed schedule timestamps should not leak parser internals",
    )


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
    require_equal(schedule_response.json()["catch_up_policy"], "run_once", "schedules should default to run_once")


def test_schedule_creation_rejects_invalid_catch_up_policy(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)

    response = client.post(
        "/schedules",
        json={
            "name": "Broken catch-up policy",
            "interval_seconds": 60,
            "catch_up_policy": "drift_forever",
            "task_title": "Scheduled task",
        },
    )
    require_equal(response.status_code, 422, "invalid catch-up policies should fail request validation")


def test_schedule_creation_rejects_naive_next_run_at(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)

    response = client.post(
        "/schedules",
        json={
            "name": "Naive schedule timestamp",
            "interval_seconds": 60,
            "task_title": "Scheduled task",
            "next_run_at": "2000-01-01T00:00:00",
        },
    )
    require_equal(response.status_code, 400, "schedule creation should reject timezone-naive next_run_at")
    require_equal(
        response.json()["detail"],
        "schedule next_run_at must include a timezone",
        "schedule timestamp errors should explain the missing timezone",
    )


def test_schedule_creation_rejects_malformed_next_run_at(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)

    response = client.post(
        "/schedules",
        json={
            "name": "Malformed schedule timestamp",
            "interval_seconds": 60,
            "task_title": "Scheduled task",
            "next_run_at": "not-a-date",
        },
    )
    require_equal(response.status_code, 400, "schedule creation should reject malformed next_run_at")
    require_equal(
        response.json()["detail"],
        "schedule next_run_at must be a valid ISO datetime",
        "schedule timestamp errors should not leak parser internals",
    )


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
