from __future__ import annotations

from copy import deepcopy
import secrets
import sqlite3
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from threading import Barrier, Event
from time import sleep
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

import hivemind.api as api_module
from hivemind.api import create_app
from hivemind.config import HivemindConfig, IntentReviewerConfig
from hivemind.oauth import SecretBox
from hivemind.policy import ProviderIntentReviewDecision, ProviderIntentReviewRequest, ProviderIntentReviewerError
from hivemind.providers import AgentProviderError, ProviderRunRequest, ProviderRunResult, ProviderToolRequest
from hivemind.store import (
    AGENT_PROVIDER_CREDENTIAL_ACTION_PREFIX,
    HivemindStore,
    LEGACY_AGENT_PROVIDER_CREDENTIAL_ACTION_PREFIX,
    SCHEDULE_BACKFILL_BATCH_LIMIT,
    StoreError,
    hash_password,
)

TEST_PASSWORD = "operator-not-secret"  # nosec B105
RECOVERY_PASSWORD = "operator-recovery-secret"  # nosec B105
PROVIDER_CREDENTIAL_ID = "cred_provider_openrouter"


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
            output_text=f"provider used {request.credential_id} with fallback env://SECONDARY_PROVIDER_SECRET",
            tool_requests=(
                ProviderToolRequest(
                    name="debug",
                    arguments={
                        "credential_id": request.credential_id,
                        "fallback_ref": "env://SECONDARY_PROVIDER_SECRET",
                        "notes": ["secondary ref env://SECONDARY_PROVIDER_SECRET"],
                        "tuple_notes": (
                            "tuple ref env://SECONDARY_PROVIDER_SECRET",
                            {"x-api-key": "tuple prefixed api key"},
                        ),
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
    assert response.status_code == 201  # nosec B101


def setup_demo(client: TestClient) -> None:
    setup(client)
    client.app.state.store.seed_demo_if_empty()


def setup_store_with_demo(store: HivemindStore, username: str = "admin") -> dict[str, object]:
    user = store.setup_admin(username, TEST_PASSWORD)
    store.seed_demo_if_empty()
    return user


def test_create_app_uses_lifespan_without_on_event_deprecation(tmp_path: Path) -> None:
    store = HivemindStore(tmp_path / "hivemind.db")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        create_app(store, start_scheduler=False)

    require_equal(
        [warning for warning in caught if "on_event is deprecated" in str(warning.message)],
        [],
        "create_app should not register deprecated FastAPI on_event handlers",
    )


def test_store_from_env_defaults_to_repo_local_dev_database(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("HIVEMIND_DB_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    store = HivemindStore.from_env()

    expected_path = tmp_path / ".data" / "hivemind.db"
    require_equal(store.db_path, expected_path, "unset HIVEMIND_DB_PATH should use the repo-local dev database")
    require_true(expected_path.is_file(), "store creation should initialize the local dev database file")


def test_store_from_env_preserves_explicit_database_path(tmp_path: Path, monkeypatch) -> None:
    expected_path = tmp_path / "explicit.db"
    monkeypatch.setenv("HIVEMIND_DB_PATH", str(expected_path))

    store = HivemindStore.from_env()

    require_equal(store.db_path, expected_path, "explicit HIVEMIND_DB_PATH should override the local dev default")
    require_true(expected_path.is_file(), "store creation should initialize the explicit database file")


def test_store_from_env_logs_active_database_path(tmp_path: Path, monkeypatch, caplog) -> None:
    expected_path = tmp_path / "hivemind.db"
    monkeypatch.setenv("HIVEMIND_DB_PATH", str(expected_path))

    with caplog.at_level(logging.INFO, logger="hivemind.runtime"):
        HivemindStore.from_env()

    require_true(str(expected_path) in caplog.text, "startup diagnostics should include the active database path")


def test_scheduler_lifespan_respects_explicit_disable(tmp_path: Path) -> None:
    app = create_app(HivemindStore(tmp_path / "hivemind.db"), start_scheduler=False)

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/health")

    require_equal(response.status_code, 200, "health check should succeed without scheduler startup")
    require_equal(getattr(app.state, "scheduler_thread", None), None, "explicit disable should not start scheduler")


def test_scheduler_lifespan_respects_env_disable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_SCHEDULER", "false")
    app = create_app(HivemindStore(tmp_path / "hivemind.db"))

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/health")

    require_equal(response.status_code, 200, "health check should succeed when env disables scheduler")
    require_equal(getattr(app.state, "scheduler_thread", None), None, "env disable should not start scheduler")


def test_scheduler_lifespan_starts_and_stops_thread(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("HIVEMIND_SCHEDULER", raising=False)
    app = create_app(HivemindStore(tmp_path / "hivemind.db"), start_scheduler=True)

    with TestClient(app, base_url="https://testserver"):
        thread = getattr(app.state, "scheduler_thread", None)
        require_true(thread is not None, "explicit enable should create scheduler thread")
        require_true(thread.is_alive(), "scheduler thread should run during app lifespan")

    require_true(not thread.is_alive(), "scheduler thread should stop after app lifespan exits")
    require_equal(getattr(app.state, "scheduler_thread", None), None, "scheduler state should be cleared after shutdown")


def test_explicit_scheduler_start_overrides_env_disable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_SCHEDULER", "false")
    app = create_app(HivemindStore(tmp_path / "hivemind.db"), start_scheduler=True)

    with TestClient(app, base_url="https://testserver"):
        thread = getattr(app.state, "scheduler_thread", None)
        require_true(thread is not None, "explicit enable should override env disable")
        require_true(thread.is_alive(), "scheduler thread should run during app lifespan")

    require_true(not thread.is_alive(), "scheduler thread should stop after app lifespan exits")


def test_scheduler_lifespan_replaces_stopping_thread_and_resumes_passes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(api_module, "SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(api_module, "SCHEDULER_INTERVAL_SECONDS", 0.01)
    store = HivemindStore(tmp_path / "hivemind.db")
    scheduler_started = Event()
    release_scheduler = Event()
    scheduler_resumed = Event()
    scheduler_calls: list[int] = []

    def blocked_schedule_pass() -> list[dict[str, object]]:
        scheduler_calls.append(len(scheduler_calls) + 1)
        if len(scheduler_calls) == 1:
            scheduler_started.set()
            release_scheduler.wait(timeout=2)
        else:
            scheduler_resumed.set()
        return []

    store.run_due_schedules_once = blocked_schedule_pass  # type: ignore[method-assign]
    app = create_app(store, start_scheduler=True)

    with TestClient(app, base_url="https://testserver"):
        first_thread = getattr(app.state, "scheduler_thread", None)
        require_true(first_thread is not None, "scheduler should start on first lifespan")
        require_true(scheduler_started.wait(timeout=1), "scheduler pass should begin")

    require_true(first_thread.is_alive(), "blocked scheduler should still be stopping after shutdown timeout")

    with TestClient(app, base_url="https://testserver"):
        replacement_thread = getattr(app.state, "scheduler_thread", None)
        require_true(replacement_thread is not None, "restart should create a replacement scheduler thread")
        require_true(replacement_thread is not first_thread, "restart should not reuse a stopping scheduler thread")
        require_true(replacement_thread.is_alive(), "replacement scheduler should run during the active lifespan")
        require_equal(len(scheduler_calls), 1, "replacement should not overlap the blocked scheduler pass")
        release_scheduler.set()
        first_thread.join(timeout=1)
        require_true(not first_thread.is_alive(), "previous scheduler should stop after its blocked pass drains")
        require_true(scheduler_resumed.wait(timeout=1), "replacement scheduler should resume passes")
        require_true(replacement_thread.is_alive(), "replacement scheduler should stay alive during the active lifespan")

    replacement_thread.join(timeout=1)
    require_true(not replacement_thread.is_alive(), "replacement scheduler should stop after lifespan exit")
    require_equal(getattr(app.state, "scheduler_thread", None), None, "stopped scheduler should clear state")


def create_provider_credential(
    store: HivemindStore,
    agent_id: str,
    *,
    credential_id: str = PROVIDER_CREDENTIAL_ID,
) -> dict[str, object]:
    return store.create_credential(
        {
            "id": credential_id,
            "name": "OpenRouter Provider Credential",
            "provider": "openrouter",
            "secret_ref": "env://OPENROUTER_API_KEY",
            "allowed_agents": [agent_id],
            "allowed_actions": [f"{AGENT_PROVIDER_CREDENTIAL_ACTION_PREFIX}openrouter"],
            "metadata": {"credential_kind": "generic_reference", "purpose": "agent_provider"},
        }
    )


def create_legacy_provider_credential(
    store: HivemindStore,
    agent_id: str,
    *,
    credential_id: str = PROVIDER_CREDENTIAL_ID,
) -> dict[str, object]:
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "id": credential_id,
        "name": "OpenRouter Provider Credential",
        "provider": "openrouter",
        "secret_ref": "env://OPENROUTER_API_KEY",
        "allowed_agents": json.dumps([agent_id]),
        "allowed_actions": json.dumps([f"{LEGACY_AGENT_PROVIDER_CREDENTIAL_ACTION_PREFIX}openrouter"]),
        "approval_required_actions": json.dumps([]),
        "max_ttl_seconds": 300,
        "require_intent": 1,
        "metadata": json.dumps({"credential_kind": "generic_reference", "purpose": "agent_provider"}),
        "created_at": now,
        "updated_at": now,
    }
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO credentials (
                id, name, provider, secret_ref, allowed_agents, allowed_actions,
                approval_required_actions, max_ttl_seconds, require_intent, metadata,
                created_at, updated_at
            )
            VALUES (
                :id, :name, :provider, :secret_ref, :allowed_agents, :allowed_actions,
                :approval_required_actions, :max_ttl_seconds, :require_intent, :metadata,
                :created_at, :updated_at
            )
            """,
            row,
        )
    return store.public_credential(store.get_credential(credential_id))


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

    assert sum(1 for status, _ in results if status == "ok") == 1  # nosec B101
    assert [detail for status, detail in results if status == "error"] == ["setup is already complete"]  # nosec B101

    conn = sqlite3.connect(db_path)
    try:
        admin_count = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin'").fetchone()[0]
        usernames = [row[0] for row in conn.execute("SELECT username FROM users")]
    finally:
        conn.close()

    assert admin_count == 1  # nosec B101
    assert len(usernames) == 1  # nosec B101


def test_auth_setup_creates_only_local_admin_by_default(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    require_equal(
        client.get("/setup-state").json(),
        {"setup_complete": False, "demo_mode": False},
        "setup state should report default demo mode as disabled",
    )
    setup(client)

    require_equal(client.get("/agents").json(), [], "default setup should not seed demo agents")
    require_equal(client.get("/credentials").json(), [], "default setup should not seed demo credentials")
    require_equal(client.get("/hives").json(), [], "default setup should not seed demo hives")


def test_auth_setup_seeds_demo_state_only_in_explicit_demo_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_DEMO_MODE", "true")
    client = client_for(tmp_path)

    require_equal(
        client.get("/setup-state").json(),
        {"setup_complete": False, "demo_mode": True},
        "setup state should report explicit demo mode",
    )
    setup(client)

    agents = client.get("/agents").json()
    credentials = client.get("/credentials").json()
    hives = client.get("/hives").json()

    require_equal(len(agents), 1, "demo mode should seed one local agent")
    require_equal(agents[0]["name"], "Scout", "demo mode should seed the Scout agent")
    require_equal(len(credentials), 1, "demo mode should seed one demo credential")
    require_equal(credentials[0]["id"], "cred_demo_github", "demo mode should seed the demo GitHub credential")
    require_equal(len(hives), 1, "demo mode should seed one local hive")


def test_frontend_is_served(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    response = client.get("/")

    assert response.status_code == 200  # nosec B101
    assert "Hivemind" in response.text  # nosec B101
    assert "/static/app.js" in response.text  # nosec B101
    for required in [
        'id="boot-view"',
        '<section id="auth-view" class="auth-shell" hidden>',
        'id="auth-error" class="field-error" role="alert" hidden',
        'name="username"',
        'name="password"',
        'name="password_confirm"',
        'autocomplete="new-password"',
        'minlength="12"',
        'id="auth-demo-mode"',
    ]:
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
    require_true(
        "Admin passwords need at least 12 non-whitespace characters" in response.text,
        "frontend should prompt for a non-blank admin password",
    )
    require_true(
        "Demo mode is on. Setup will also create Scout and a demo GitHub credential policy." in response.text,
        "frontend should include explicit demo-mode setup copy",
    )
    for required in [
        'id="stale-heartbeat-count"',
        'id="missing-heartbeat-count"',
        'id="heartbeat-alert-list"',
        'id="heartbeats-list"',
        'id="runtime-health-panel"',
        'id="runtime-stale-heartbeats-count"',
        'id="runtime-due-schedules-count"',
    ]:
        if required not in response.text:
            raise AssertionError(f"missing expected heartbeat overview markup: {required}")


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
    require_true("setupKnown: false" in response.text, "frontend should start without assuming setup is incomplete")
    require_true("demoMode: false" in response.text, "frontend should default demo-mode state to disabled")
    require_true("state.demoMode = Boolean(setup.demo_mode)" in response.text, "frontend should read demo mode from setup state")
    require_true(
        "const hadSetupState = state.setupKnown" in response.text,
        "frontend should remember whether setup state was already loaded",
    )
    require_true(
        "if (!hadSetupState)" in response.text,
        "frontend should only return to boot mode when the initial setup-state load fails",
    )
    require_true(
        "state.setupKnown = false;" not in response.text,
        "frontend should not collapse loaded sessions into boot mode after transient setup-state failures",
    )
    require_true(
        "let runtimePayload;" in response.text,
        "frontend should stage runtime API results before replacing loaded state",
    )
    require_true(
        "runtimePayload = await Promise.all" in response.text,
        "frontend should treat runtime API loading as a recoverable batch",
    )
    require_true(
        "const [config, queenBee, hives, agents, toolActions, credentials, oauthProviders, leases, tasks, schedules, heartbeats, auditEvents, runtime] = runtimePayload" in response.text,
        "frontend should render a recoverable shell if runtime data loading fails",
    )
    require_true('api("/queen-bee")' in response.text, "frontend should fetch the Queen Bee operator profile")
    require_true("loadRuntimeOverview()" in response.text, "frontend should fetch runtime overview without breaking core loading")
    require_true("function validateAuthPayload" in response.text, "frontend should validate auth form state")
    require_true("admin password must include at least 12 non-whitespace characters" in response.text, "frontend should reject blank setup passwords")
    require_true("new Error(body.detail" not in response.text, "API helper should not stringify structured errors")


def test_credentials_frontend_route_is_served(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    response = client.get("/control/credentials")

    require_equal(response.status_code, 200, "credentials route should be served")
    require_true('data-page-link="credentials"' in response.text, "credentials nav link should be present")
    require_true("credential broker" in response.text, "credential page heading should be present")
    require_true('id="credential-template-picker"' in response.text, "credential template picker should be present")
    require_true('id="credential-template-fields"' in response.text, "credential template fields should be present")
    require_true('id="tool-actions-list"' in response.text, "tool action list should be present")
    require_true('name="approval_required_actions"' in response.text, "approval policy field should be present")
    require_true('name="agent_lease_limit"' in response.text, "agent lease limit field should be present")
    require_true('name="credential_action_limit"' in response.text, "action limit field should be present")
    require_true('id="pending-approvals-list"' in response.text, "pending approvals list should be present")


def test_hives_frontend_route_is_served(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    response = client.get("/control/hives")
    script_response = client.get("/static/app.js")

    require_equal(response.status_code, 200, "hives frontend route should render")
    require_equal(script_response.status_code, 200, "frontend script should render")
    require_true('data-page-link="hives"' in response.text, "hives route should expose navigation")
    require_true('id="hive-form"' in response.text, "hives route should expose the hive form")
    require_true('id="hive-issue-form"' in response.text, "hives route should expose issue request form")
    require_true(
        'name="issue_rate_limit_per_hour"' in response.text,
        "hives route should expose issue rate controls",
    )
    require_true('name="can_spawn_subagents"' in response.text, "hives route should expose subagent controls")
    require_true(
        "$('#hive-issue-form select[name=\"hive_id\"]').innerHTML = optionList(state.hives);"
        in script_response.text,
        "issue request hive selector should default to a concrete hive",
    )
    require_true(
        'toast("Create a hive before queueing issue requests.")' in script_response.text,
        "issue request flow should guard missing hives before posting",
    )


def test_frontend_renders_task_operator_controls(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    response = client.get("/")

    assert response.status_code == 200  # nosec B101
    for required in [
        'id="running-task-count"',
        'id="blocked-task-count"',
        'id="due-schedule-count"',
        'id="stale-heartbeat-count"',
        'id="task-health"',
        'name="status"',
    ]:
        assert required in response.text  # nosec B101


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
    require_equal(
        setup_state.json(),
        {"setup_complete": False, "demo_mode": False},
        "mismatch should not complete setup",
    )
    require_equal(setup_response.status_code, 201, "matching confirmation should create the admin")
    require_equal(setup_response.json()["user"]["role"], "admin", "first user should be admin")


def test_setup_rejects_whitespace_only_admin_password(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    blank_password_response = client.post(
        "/auth/setup",
        json={
            "username": "admin",
            "password": " " * 12,
            "password_confirm": " " * 12,
        },
    )
    setup_state = client.get("/setup-state")

    require_equal(blank_password_response.status_code, 400, "setup should reject blank admin passwords")
    require_equal(
        blank_password_response.json(),
        {"detail": "admin password must include at least 12 non-whitespace characters"},
        "setup should explain the password rule",
    )
    require_equal(
        setup_state.json(),
        {"setup_complete": False, "demo_mode": False},
        "blank password should not complete setup",
    )


def test_admin_recovery_resets_setup_complete_admin_password_and_revokes_sessions(tmp_path: Path) -> None:
    store = HivemindStore(tmp_path / "recovery.db")
    admin = store.setup_admin("admin", TEST_PASSWORD)
    token, _ = store.login("admin", TEST_PASSWORD)

    recovered = store.reset_admin_password("ADMIN", RECOVERY_PASSWORD)

    require_equal(recovered["id"], admin["id"], "recovery should reset the existing admin account")
    require_equal(recovered["username"], "admin", "recovery should normalize the admin username")
    require_equal(store.get_session_user(token), None, "recovery should revoke active admin sessions")
    try:
        store.login("admin", TEST_PASSWORD)
    except StoreError as exc:
        require_equal(str(exc), "invalid username or password", "old password should no longer authenticate")
    else:
        raise AssertionError("old admin password should not authenticate after recovery")

    _, user = store.login("admin", RECOVERY_PASSWORD)
    require_equal(user["id"], admin["id"], "new password should authenticate the recovered admin")

    audit_events = store.list_audit_events()
    recovery_events = [event for event in audit_events if event["type"] == "auth.admin_recovery.password_reset"]
    require_equal(len(recovery_events), 1, "recovery should write one audit event")
    event = recovery_events[0]
    require_equal(event["actor_id"], "operator.local_recovery", "recovery audit should use an explicit local actor")
    require_equal(event["target_id"], admin["id"], "recovery audit should target the reset admin")
    require_equal(event["decision"], "allowed", "successful recovery should be audited as allowed")
    require_equal(event["metadata"]["username"], "admin", "recovery audit should include the admin username")
    require_equal(event["metadata"]["sessions_revoked"], 1, "recovery audit should count revoked sessions")
    require_true(RECOVERY_PASSWORD not in json.dumps(event), "recovery audit should not expose the password")


def test_admin_recovery_fails_closed_for_unknown_admin(tmp_path: Path) -> None:
    store = HivemindStore(tmp_path / "recovery.db")
    store.setup_admin("admin", TEST_PASSWORD)

    try:
        store.reset_admin_password("missing-admin", RECOVERY_PASSWORD)
    except StoreError as exc:
        require_equal(str(exc), "admin user not found", "recovery should reject unknown admin users")
    else:
        raise AssertionError("recovery should reject unknown admin users")

    require_equal(store.login("admin", TEST_PASSWORD)[1]["username"], "admin", "failed recovery should not alter admin auth")


def test_admin_recovery_fails_closed_for_non_admin_user(tmp_path: Path) -> None:
    store = HivemindStore(tmp_path / "recovery.db")
    store.setup_admin("admin", TEST_PASSWORD)
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO users (id, username, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
            ("user_member", "member", hash_password(TEST_PASSWORD), "member", "2026-01-01T00:00:00+00:00"),
        )

    try:
        store.reset_admin_password("member", RECOVERY_PASSWORD)
    except StoreError as exc:
        require_equal(str(exc), "admin user not found", "recovery should reject non-admin users")
    else:
        raise AssertionError("recovery should reject non-admin users")

    require_equal(store.login("member", TEST_PASSWORD)[1]["username"], "member", "failed recovery should not alter member auth")


def test_setup_counts_only_non_whitespace_admin_password_characters(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    short_password_response = client.post(
        "/auth/setup",
        json={
            "username": "admin",
            "password": "a b c d e f g",
            "password_confirm": "a b c d e f g",
        },
    )
    setup_state = client.get("/setup-state")

    require_equal(
        short_password_response.status_code,
        400,
        "setup should reject passwords with fewer than 12 non-whitespace characters",
    )
    require_equal(
        short_password_response.json(),
        {"detail": "admin password must include at least 12 non-whitespace characters"},
        "setup should count internal whitespace as policy padding",
    )
    require_equal(
        setup_state.json(),
        {"setup_complete": False, "demo_mode": False},
        "short non-whitespace password should not complete setup",
    )


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

    assert setup_response.status_code == 201  # nosec B101
    assert logout_response.status_code == 200  # nosec B101
    assert login_response.status_code == 200  # nosec B101
    for response in (setup_response, login_response, logout_response):
        set_cookie = response.headers["set-cookie"]
        assert "HttpOnly" in set_cookie  # nosec B101
        assert "SameSite=lax" in set_cookie  # nosec B101
        assert "Secure" in set_cookie  # nosec B101


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

    assert setup_response.status_code == 201  # nosec B101
    assert me_response.status_code == 200  # nosec B101
    assert logout_response.status_code == 200  # nosec B101
    assert login_response.status_code == 200  # nosec B101
    for response in (setup_response, login_response, logout_response):
        set_cookie = response.headers["set-cookie"]
        assert "HttpOnly" in set_cookie  # nosec B101
        assert "SameSite=lax" in set_cookie  # nosec B101
        assert "Secure" not in set_cookie  # nosec B101


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

    assert client.get("/config").status_code == 401  # nosec B101
    setup_demo(client)
    response = client.get("/config")
    reviewer = response.json()["intent_reviewer"]
    credential = client.get("/credentials").json()[0]

    assert response.status_code == 200  # nosec B101
    assert reviewer["provider"] == "local"  # nosec B101
    assert reviewer["credential_ref_preview"] == "env://HIV..."  # nosec B101
    assert reviewer["credential_ref_preview"] == credential["secret_ref_preview"]  # nosec B101
    assert "credential_ref" not in reviewer  # nosec B101
    assert "HIVEMIND_DEMO_GITHUB_TOKEN" not in response.text  # nosec B101


def test_config_exposes_redacted_provider_backed_reviewer_settings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_INTENT_REVIEWER_PROVIDER", "openrouter")
    monkeypatch.setenv("HIVEMIND_INTENT_REVIEWER_MODEL", "anthropic/claude-sonnet-4")
    monkeypatch.setenv("HIVEMIND_INTENT_REVIEWER_CREDENTIAL_REF", "env://OPENROUTER_API_KEY")
    client = client_for(tmp_path)

    setup_demo(client)
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
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_ID", PROVIDER_CREDENTIAL_ID)
    client = client_for(tmp_path)

    setup_demo(client)
    response = client.get("/config")
    providers = {provider["provider"]: provider for provider in response.json()["agent_providers"]}
    expected_providers = {"openai", "codex", "claude", "gemini", "openrouter", "bedrock", "huggingface", "ollama"}

    require_equal(response.status_code, 200, "config should return after setup")
    require_true(expected_providers.issubset(providers), "config should list the supported remote agent providers")
    require_equal(providers["openrouter"]["model"], "anthropic/claude-sonnet-4", "provider config should expose the configured model")
    require_equal(providers["openrouter"]["credential_id"], PROVIDER_CREDENTIAL_ID, "provider config should expose credential IDs")
    require_true("credential_ref" not in providers["openrouter"], "provider config should not expose raw credential refs")
    require_true("credential_ref_preview" not in providers["openrouter"], "provider config should not expose secret ref previews")
    require_true("OPENROUTER_API_KEY" not in response.text, "config should not expose raw provider credential refs")


def test_config_rejects_agent_provider_secret_ref_env(monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_REF", "env://OPENROUTER_API_KEY")

    try:
        HivemindConfig.from_env()
    except ValueError as exc:
        require_true("CREDENTIAL_ID" in str(exc), "agent provider config should direct operators to credential IDs")
    else:
        raise AssertionError("agent provider credential_ref env var was accepted")


def test_spawn_agent_uses_provider_config_model_when_model_is_omitted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_MODEL", "anthropic/claude-sonnet-4")
    client = client_for(tmp_path)
    setup_demo(client)

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


def test_spawn_agent_rejects_secret_like_system_prompt(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    private_key_prompt = "-----BEGIN " + "PRIVATE KEY-----\nabc123\n-----END " + "PRIVATE KEY-----"

    prompts = (
        "Use env://OPENROUTER_API_KEY when calling the provider.",
        "password = hunter2",
        "Authorization: Bearer raw-provider-token-value",
        private_key_prompt,
        "sk-proj-raw-provider-token-value",
    )
    for index, system_prompt in enumerate(prompts):
        response = client.post(
            "/agents",
            json={
                "name": f"unsafe prompt {index}",
                "role": "try to persist secret-like prompt material",
                "provider": "local",
                "model": "deterministic-policy",
                "system_prompt": system_prompt,
            },
        )

        require_equal(response.status_code, 400, "agent prompt creation should reject secret-like material")
        require_true(
            "system_prompt contains secret-like material" in response.json()["detail"],
            "rejection should identify the unsafe prompt field",
        )
        require_true("OPENROUTER_API_KEY" not in response.text, "rejection should not echo credential ref targets")
        require_true("hunter2" not in response.text, "rejection should not echo password-like values")
        require_true("raw-provider-token" not in response.text, "rejection should not echo token-like values")


def test_store_create_agent_rejects_secret_like_system_prompt(tmp_path: Path) -> None:
    store = HivemindStore(tmp_path / "hivemind.db")
    store.setup_admin("admin", TEST_PASSWORD)

    try:
        store.create_agent(
            {
                "name": "Unsafe local agent",
                "role": "try to persist raw credential material",
                "provider": "local",
                "model": "deterministic-policy",
                "system_prompt": "apiKey = raw-provider-token-value",
            }
        )
    except StoreError as exc:
        require_true("system_prompt contains secret-like material" in str(exc), "store should reject unsafe prompts")
        require_true("raw-provider-token" not in str(exc), "store error should not echo token-like values")
    else:
        raise AssertionError("store accepted secret-like agent prompt")


def test_spawn_agent_allows_safe_capability_guidance_prompt(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    system_prompt = "Use brokered capabilities when a task needs credentials. Keep updates concise."

    response = client.post(
        "/agents",
        json={
            "name": "safe prompt",
            "role": "request credential capabilities through policy",
            "provider": "local",
            "model": "deterministic-policy",
            "system_prompt": system_prompt,
        },
    )
    agents = client.get("/agents").json()

    require_equal(response.status_code, 201, "safe prompt content should still be accepted")
    require_equal(response.json()["system_prompt"], system_prompt, "safe prompt should remain visible on create")
    require_equal(agents[0]["system_prompt"], system_prompt, "safe prompt should remain visible on list")


def test_agents_public_responses_redact_legacy_secret_like_system_prompt(tmp_path: Path) -> None:
    store = HivemindStore(tmp_path / "hivemind.db")
    setup_store_with_demo(store)
    agent = store.list_agents()[0]
    unsafe_prompt = "Use env://OPENROUTER_API_KEY with password = hunter2."
    with store.connect() as conn:
        conn.execute("UPDATE agents SET system_prompt = ? WHERE id = ?", (unsafe_prompt, agent["id"]))
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    login = client.post("/auth/login", json={"username": "admin", "password": TEST_PASSWORD})

    response = client.get("/agents")

    require_equal(login.status_code, 200, "operator login should succeed")
    require_equal(response.status_code, 200, "agents list should be returned")
    require_equal(response.json()[0]["system_prompt"], "[redacted]", "unsafe legacy prompts should be redacted")
    require_true("OPENROUTER_API_KEY" not in response.text, "agents response should not expose credential ref targets")
    require_true("hunter2" not in response.text, "agents response should not expose password-like values")


def test_authenticated_jit_lease_flow_redacts_secret_ref(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    agent = client.get("/agents").json()[0]
    credential = client.get("/credentials").json()[0]
    payload_key = f"token-{secrets.token_hex(8)}"
    payload_secret = f"demo-{secrets.token_hex(8)}"

    assert "HIVEMIND_DEMO_GITHUB_TOKEN" not in credential["secret_ref_preview"]  # nosec B101
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
    assert lease_response.status_code == 201  # nosec B101
    lease = lease_response.json()
    assert lease["lease_token"].startswith("hvl_")  # nosec B101

    action_response = client.post(
        "/credential-actions",
        json={
            "lease_token": lease["lease_token"],
            "action": "read_repo",
            "payload": {"repo": "hivemind", payload_key: payload_secret},
        },
    )
    assert action_response.status_code == 200  # nosec B101
    assert action_response.json()["ok"] is True  # nosec B101
    audit_events = client.get("/audit-events").json()
    performed_event = next(event for event in audit_events if event["type"] == "credential.action.performed")
    require_equal(performed_event["actor_id"], agent["id"], "performed audit event should identify the agent")
    require_equal(performed_event["target_id"], credential["id"], "performed audit event should identify the credential")
    require_equal(
        performed_event["metadata"],
        {"action": "read_repo", "payload_key_count": 2},
        "performed audit metadata should include action and payload key count only",
    )
    require_true(payload_key not in str(audit_events), "audit events should not expose payload keys")
    require_true(payload_secret not in str(audit_events), "audit events should not expose payload values")


def test_denied_credential_action_is_audited_without_payload_values(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    agent = client.get("/agents").json()[0]
    credential = client.get("/credentials").json()[0]
    payload_key = f"token-{secrets.token_hex(8)}"
    payload_secret = f"demo-{secrets.token_hex(8)}"

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
    require_equal(lease_response.status_code, 201, "lease request should succeed before denied action")
    lease = lease_response.json()

    action_response = client.post(
        "/credential-actions",
        json={
            "lease_token": lease["lease_token"],
            "action": "delete_repo",
            "payload": {"repo": "hivemind", payload_key: payload_secret},
        },
    )
    require_equal(action_response.status_code, 403, "wrong action should be denied")
    require_equal(
        action_response.json()["detail"],
        "credential lease does not allow this action",
        "wrong action denial should explain the lease action mismatch",
    )

    audit_events = client.get("/audit-events").json()
    denied_event = next(event for event in audit_events if event["type"] == "credential.action.denied")
    require_equal(denied_event["decision"], "denied", "denied action audit event should record the decision")
    require_equal(denied_event["actor_id"], agent["id"], "denied action audit event should identify the agent")
    require_equal(denied_event["target_id"], credential["id"], "denied action audit event should identify the credential")
    require_equal(
        {
            "action": denied_event["metadata"]["action"],
            "lease_id": denied_event["metadata"]["lease_id"],
            "payload_key_count": denied_event["metadata"]["payload_key_count"],
        },
        {"action": "delete_repo", "lease_id": lease["id"], "payload_key_count": 2},
        "denied action audit metadata should include action, lease id, and payload key count only",
    )
    require_true(payload_key not in str(audit_events), "audit events should not expose denied payload keys")
    require_true(payload_secret not in str(audit_events), "audit events should not expose denied payload values")


def test_denied_credential_lease_unknown_references_are_audited(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    agent = client.get("/agents").json()[0]

    missing_agent_response = client.post(
        "/credential-leases",
        json={
            "credential_id": "cred_missing",
            "agent_id": "agent_missing",
            "action": "read_repo",
            "intent": "Read repository metadata for safe task triage.",
            "ttl_seconds": 30,
        },
    )
    require_equal(missing_agent_response.status_code, 403, "unknown agent lease requests should fail closed")
    require_equal(
        missing_agent_response.json()["detail"],
        "unknown agent: agent_missing",
        "unknown agent denial should identify the bad agent reference",
    )

    missing_credential_response = client.post(
        "/credential-leases",
        json={
            "credential_id": "cred_missing",
            "agent_id": agent["id"],
            "action": "read_repo",
            "intent": "Read repository metadata for safe task triage.",
            "ttl_seconds": 30,
        },
    )
    require_equal(missing_credential_response.status_code, 403, "unknown credential lease requests should fail closed")
    require_equal(
        missing_credential_response.json()["detail"],
        "unknown credential: cred_missing",
        "unknown credential denial should identify the bad credential reference",
    )

    audit_events = client.get("/audit-events").json()
    require_true(
        any(
            event["type"] == "credential.lease.denied"
            and event["actor_id"] == "agent_missing"
            and event["target_id"] == "cred_missing"
            and event["reason"] == "unknown agent: agent_missing"
            and event["metadata"] == {"action": "read_repo"}
            for event in audit_events
        ),
        "unknown agent denial should be audited",
    )
    require_true(
        any(
            event["type"] == "credential.lease.denied"
            and event["actor_id"] == agent["id"]
            and event["target_id"] == "cred_missing"
            and event["reason"] == "unknown credential: cred_missing"
            and event["metadata"] == {"action": "read_repo"}
            for event in audit_events
        ),
        "unknown credential denial should be audited",
    )

def test_denied_credential_lease_redacts_unsafe_action_identifier(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    agent = client.get("/agents").json()[0]
    credential = client.get("/credentials").json()[0]
    unsafe_action = f"token-{secrets.token_hex(8)}"

    response = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": agent["id"],
            "action": unsafe_action,
            "intent": "Read repository metadata for safe task triage.",
            "ttl_seconds": 30,
        },
    )

    require_equal(response.status_code, 403, "unsafe action should be denied")
    require_equal(
        response.json()["detail"],
        "unknown tool action: <redacted>",
        "unsafe unknown action denial should redact the action identifier",
    )

    audit_events = client.get("/audit-events").json()
    denied_event = next(
        event
        for event in audit_events
        if event["type"] == "credential.lease.denied"
        and event["reason"] == "unknown tool action: <redacted>"
    )
    require_equal(
        denied_event["metadata"],
        {"action": "<redacted>"},
        "unsafe action identifier should be redacted in audit metadata",
    )
    require_true(unsafe_action not in str(audit_events), "unsafe action identifier should not appear in audit events")


def test_unknown_lease_agent_is_rejected(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
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


def test_tool_action_registry_lists_builtins_and_persists_custom_actions(tmp_path: Path) -> None:
    db_path = tmp_path / "tool-actions.db"
    store = HivemindStore(db_path)
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup_demo(client)

    builtins = client.get("/tool-actions")
    create_response = client.post(
        "/tool-actions",
        json={
            "name": "repo_status",
            "description": "Read repository status.",
            "required_credential_action": "read_repo",
            "risk_level": "low",
            "input_schema": {
                "type": "object",
                "properties": {"repo": {"type": "string"}},
                "required": ["repo"],
                "additionalProperties": False,
            },
        },
    )
    restarted = TestClient(create_app(HivemindStore(db_path), start_scheduler=False), base_url="https://testserver")
    restarted_login = restarted.post("/auth/login", json={"username": "admin", "password": TEST_PASSWORD})
    persisted = restarted.get("/tool-actions")

    require_equal(builtins.status_code, 200, "tool action registry should be readable after setup")
    require_true(any(action["name"] == "read_repo" for action in builtins.json()), "registry should seed read_repo")
    require_equal(create_response.status_code, 201, "custom tool action should be persisted")
    require_equal(create_response.json()["name"], "repo_status", "custom action name should normalize")
    require_equal(restarted_login.status_code, 200, "restarted client should authenticate")
    require_true(any(action["name"] == "repo_status" for action in persisted.json()), "custom tool action should survive store restart")


def test_tool_action_registry_rejects_inconsistent_input_schemas(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)

    missing_required = client.post(
        "/tool-actions",
        json={
            "name": "broken_required",
            "description": "Invalid schema.",
            "required_credential_action": "read_repo",
            "risk_level": "low",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": ["repo"],
                "additionalProperties": False,
            },
        },
    )
    unsupported_type = client.post(
        "/tool-actions",
        json={
            "name": "broken_type",
            "description": "Invalid property type.",
            "required_credential_action": "read_repo",
            "risk_level": "low",
            "input_schema": {
                "type": "object",
                "properties": {"repo": {"type": "text"}},
                "required": ["repo"],
                "additionalProperties": False,
            },
        },
    )
    actions = {action["name"] for action in client.get("/tool-actions").json()}

    require_equal(missing_required.status_code, 400, "tool actions should reject required fields missing from properties")
    require_equal(
        missing_required.json()["detail"],
        "tool action input_schema required field is not declared in properties: repo",
        "missing required field error should name the invalid field",
    )
    require_equal(unsupported_type.status_code, 400, "tool actions should reject unsupported schema property types")
    require_equal(
        unsupported_type.json()["detail"],
        "tool action input_schema property type is unsupported: repo",
        "unsupported schema type error should name the invalid property",
    )
    require_true("broken_required" not in actions, "invalid required schema should not be persisted")
    require_true("broken_type" not in actions, "invalid type schema should not be persisted")


def test_tool_action_registry_migrates_existing_credential_actions(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-tool-actions.db"
    store = HivemindStore(db_path)
    agent = store.create_agent({"name": "Legacy Worker", "role": "Carry forward existing local scopes."})
    credential = store.create_credential(
        {
            "name": "Legacy GitHub Capability",
            "provider": "github",
            "secret_ref": "env://LEGACY_GITHUB_TOKEN",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["archive_repo"],
            "approval_required_actions": ["archive_repo"],
            "max_ttl_seconds": 60,
            "require_intent": True,
            "metadata": {},
        }
    )
    restarted = HivemindStore(db_path)
    actions = restarted.list_tool_actions()
    token, lease = restarted.request_lease(
        credential["id"],
        agent["id"],
        "archive_repo",
        "Archive a repository after explicit operator approval.",
        30,
    )

    migrated = next(action for action in actions if action["name"] == "archive_repo")
    require_equal(migrated["required_credential_action"], "archive_repo", "legacy action should map to its existing credential scope")
    require_equal(migrated["risk_level"], "medium", "legacy migrated action should use a conservative risk level")
    require_equal(token, None, "approval-required migrated action should not return a token before approval")
    require_equal(lease["action"], "archive_repo", "migrated action should be usable for lease requests")


def test_tool_action_registry_skips_unsafe_legacy_actions(tmp_path: Path) -> None:
    db_path = tmp_path / "unsafe-legacy-tool-actions.db"
    store = HivemindStore(db_path)
    agent = store.create_agent({"name": "Legacy Worker", "role": "Carry forward safe local scopes."})
    now = datetime.now(timezone.utc).isoformat()
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO credentials (
                id, name, provider, secret_ref, allowed_agents, allowed_actions,
                approval_required_actions, max_ttl_seconds, require_intent, metadata,
                created_at, updated_at
            )
            VALUES (
                :id, :name, :provider, :secret_ref, :allowed_agents, :allowed_actions,
                :approval_required_actions, :max_ttl_seconds, :require_intent, :metadata,
                :created_at, :updated_at
            )
            """,
            {
                "id": "cred_unsafe_legacy",
                "name": "Unsafe Legacy Capability",
                "provider": "github",
                "secret_ref": "env://LEGACY_GITHUB_TOKEN",
                "allowed_agents": json.dumps([agent["id"]]),
                "allowed_actions": json.dumps(["archive_repo", "token-unsafe"]),
                "approval_required_actions": json.dumps(["token-unsafe"]),
                "max_ttl_seconds": 60,
                "require_intent": 1,
                "metadata": json.dumps({}),
                "created_at": now,
                "updated_at": now,
            },
        )

    restarted = HivemindStore(db_path)
    action_names = {action["name"] for action in restarted.list_tool_actions()}

    require_true("archive_repo" in action_names, "safe legacy action should migrate into the tool registry")
    require_true("token-unsafe" not in action_names, "unsafe legacy action should not broaden the tool registry")


def test_queen_bee_profile_and_tool_catalog_are_auth_guarded_and_safe(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    unauthenticated = client.get("/queen-bee")
    require_equal(unauthenticated.status_code, 401, "Queen Bee profile should require operator auth")

    setup(client)

    profile_response = client.get("/queen-bee")
    require_equal(profile_response.status_code, 200, "authenticated operators should see Queen Bee profile")
    profile = profile_response.json()

    require_equal(profile["id"], "agent_queen_bee", "Queen Bee should have a stable first-party agent id")
    require_equal(profile["name"], "Queen Bee", "Queen Bee profile should name the operator agent")
    require_equal(profile["provisioned"], False, "Queen Bee should not be silently seeded during setup")
    require_equal(client.get("/agents").json(), [], "first-run setup should still create only the local admin account")

    tools = {tool["name"]: tool for tool in profile["tools"]}
    for expected_tool in ("read_public_config", "create_task", "create_credential_reference", "import_declarative_config"):
        require_true(expected_tool in tools, f"Queen Bee catalog should include {expected_tool}")
    require_true(
        "secret_value" not in json.dumps(tools["create_credential_reference"]),
        "Queen Bee credential-reference tool should not advertise raw secret input",
    )


def test_queen_bee_provisioning_creates_explicit_operator_agent(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)

    provision_response = client.post("/queen-bee/provision")
    require_equal(provision_response.status_code, 201, "operator should explicitly provision Queen Bee")
    queen = provision_response.json()

    require_equal(queen["id"], "agent_queen_bee", "provisioned Queen Bee should use the stable id")
    require_equal(queen["name"], "Queen Bee", "provisioned agent should be Queen Bee")
    require_equal(queen["status"], "idle", "provisioned Queen Bee should start idle")
    require_true(
        "first-party tools" in queen["system_prompt"],
        "Queen Bee prompt should route operation through first-party tools",
    )

    second_response = client.post("/queen-bee/provision")
    require_equal(second_response.status_code, 201, "re-provisioning Queen Bee should be idempotent")
    require_equal(second_response.json()["id"], queen["id"], "re-provisioning should return the existing agent")

    agents = client.get("/agents").json()
    require_equal([agent["id"] for agent in agents], ["agent_queen_bee"], "Queen Bee should be the only explicit agent")


def test_queen_bee_tool_creates_task_with_agent_attributed_audit(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    operator = client.get("/me").json()
    queen = client.post("/queen-bee/provision").json()
    scout = next(agent for agent in client.get("/agents").json() if agent["name"] == "Scout")
    credential = client.get("/credentials").json()[0]

    response = client.post(
        "/queen-bee/tools/create_task",
        json={
            "arguments": {
                "title": "Inspect broker policy",
                "description": "Review the JIT policy surface through the Queen Bee tool.",
                "priority": "high",
                "assigned_agent_id": scout["id"],
                "credential_id": credential["id"],
                "action": "read_repo",
                "intent": "Inspect the broker policy without exposing raw secrets.",
                "heartbeat_seconds": 60,
            }
        },
    )

    require_equal(response.status_code, 200, "Queen Bee create_task tool should succeed")
    payload = response.json()
    require_equal(payload["actor_id"], queen["id"], "tool response should identify Queen Bee as actor")
    task = payload["result"]
    require_equal(task["title"], "Inspect broker policy", "tool should return the created task")
    require_equal(task["assigned_agent_id"], scout["id"], "tool should preserve assigned agent")

    audit_events = client.get("/audit-events").json()
    task_created = next(event for event in audit_events if event["type"] == "task.created" and event["target_id"] == task["id"])
    require_equal(task_created["actor_id"], queen["id"], "task creation should be attributed to Queen Bee")
    tool_audit = next(event for event in audit_events if event["type"] == "queen_bee.tool.executed")
    require_equal(tool_audit["actor_id"], queen["id"], "tool audit should be attributed to Queen Bee")
    require_equal(tool_audit["metadata"]["operator_id"], operator["id"], "tool audit should retain the invoking operator")
    require_equal(tool_audit["metadata"]["tool_name"], "create_task", "tool audit should name the tool")


def test_queen_bee_credential_tool_rejects_raw_secret_payloads(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)
    queen = client.post("/queen-bee/provision").json()

    response = client.post(
        "/queen-bee/tools/create_credential_reference",
        json={
            "arguments": {
                "name": "Unsafe inline token",
                "provider": "github",
                "secret_ref": "env://HIVEMIND_OPERATOR_TOKEN",
                "secret_value": "inline-sensitive-material",
                "allowed_agents": [queen["id"]],
                "allowed_actions": ["read_repo"],
                "metadata": {"credential_kind": "generic_reference"},
            }
        },
    )

    require_equal(response.status_code, 400, "Queen Bee credential tool should reject raw secret fields")
    require_true(
        "inline-sensitive-material" not in response.text,
        "raw secret values must not be reflected in validation responses",
    )

    audit_events = client.get("/audit-events").json()
    require_true(
        "inline-sensitive-material" not in json.dumps(audit_events),
        "raw secret values must not be written to audit events",
    )
    denied = next(event for event in audit_events if event["type"] == "queen_bee.tool.denied")
    require_equal(denied["actor_id"], queen["id"], "denied tool use should be attributed to Queen Bee once provisioned")
    require_equal(denied["decision"], "denied", "denied tool audit should be explicit")


def test_queen_bee_creates_secret_reference_credentials_without_raw_secret_exposure(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup(client)
    queen = client.post("/queen-bee/provision").json()

    response = client.post(
        "/queen-bee/tools/create_credential_reference",
        json={
            "arguments": {
                "name": "Operator repo reader",
                "provider": "github",
                "secret_ref": "env://HIVEMIND_OPERATOR_TOKEN",
                "allowed_agents": [queen["id"]],
                "allowed_actions": ["read_repo"],
                "approval_required_actions": ["read_repo"],
                "max_ttl_seconds": 120,
                "require_intent": True,
                "metadata": {"credential_kind": "generic_reference"},
            }
        },
    )

    require_equal(response.status_code, 200, "Queen Bee should create external secret-reference credentials")
    credential = response.json()["result"]
    require_equal(credential["name"], "Operator repo reader", "credential result should name the policy")
    require_equal(credential["policy"]["allowed_agents"], [queen["id"]], "credential should be scoped to Queen Bee")
    require_equal(credential["policy"]["approval_required_actions"], ["read_repo"], "approval gate should be preserved")
    require_true("secret_ref" not in credential, "public credential result should not expose raw secret_ref")
    require_true(
        credential["secret_ref_preview"].startswith("env://HIV..."),
        "public credential result should only expose a secret ref preview",
    )


def test_unknown_tool_action_is_denied_before_lease_issuance(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    agent = client.get("/agents").json()[0]
    credential = client.get("/credentials").json()[0]

    response = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": agent["id"],
            "action": "delete_repo",
            "intent": "Delete a repository even though no registered tool action allows it.",
            "ttl_seconds": 30,
        },
    )
    audit_events = client.get("/audit-events").json()

    require_equal(response.status_code, 403, "unknown tool actions should fail closed")
    require_equal(response.json()["detail"], "unknown tool action: delete_repo", "denial should identify the unknown action")
    require_true(
        any(
            event["type"] == "credential.lease.denied"
            and event["reason"] == "unknown tool action: delete_repo"
            and event["metadata"]["action"] == "delete_repo"
            for event in audit_events
        ),
        "unknown tool action should be audited as a denied lease request",
    )


def test_tool_action_maps_to_required_credential_action_and_validates_payload(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    agent = client.get("/agents").json()[0]
    credential = client.get("/credentials").json()[0]
    tool_response = client.post(
        "/tool-actions",
        json={
            "name": "repo_status",
            "description": "Read repository status through the read_repo scope.",
            "required_credential_action": "read_repo",
            "risk_level": "low",
            "input_schema": {
                "type": "object",
                "properties": {"repo": {"type": "string"}},
                "required": ["repo"],
                "additionalProperties": False,
            },
        },
    )
    lease_response = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": agent["id"],
            "action": "repo_status",
            "intent": "Read repository status for implementation triage.",
            "ttl_seconds": 30,
        },
    )
    invalid_action = client.post(
        "/credential-actions",
        json={"lease_token": lease_response.json()["lease_token"], "action": "repo_status", "payload": {}},
    )
    audit_after_invalid = client.get("/audit-events").json()
    valid_action = client.post(
        "/credential-actions",
        json={"lease_token": lease_response.json()["lease_token"], "action": "repo_status", "payload": {"repo": "hivemind"}},
    )

    require_equal(tool_response.status_code, 201, "custom mapped tool action should be created")
    require_equal(lease_response.status_code, 201, "tool action should issue through the required credential action")
    require_equal(lease_response.json()["action"], "repo_status", "lease should bind to the exact tool action")
    require_equal(invalid_action.status_code, 403, "invalid payload should fail before broker acceptance")
    require_equal(invalid_action.json()["detail"], "payload missing required field: repo", "missing required field should be reported")
    require_true(
        not any(event["type"] == "credential.action.performed" and event["metadata"]["action"] == "repo_status" for event in audit_after_invalid),
        "invalid payload should not write a success audit event",
    )
    require_equal(valid_action.status_code, 200, "valid payload should be accepted")
    require_equal(valid_action.json()["credential_action"], "read_repo", "result should expose the required credential action")


def test_lease_for_one_tool_action_cannot_execute_another_with_same_credential_scope(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    agent = client.get("/agents").json()[0]
    credential = client.get("/credentials").json()[0]
    for action_name in ("repo_status", "repo_summary"):
        response = client.post(
            "/tool-actions",
            json={
                "name": action_name,
                "description": f"Read {action_name}.",
                "required_credential_action": "read_repo",
                "risk_level": "low",
                "input_schema": {
                    "type": "object",
                    "properties": {"repo": {"type": "string"}},
                    "required": ["repo"],
                    "additionalProperties": True,
                },
            },
        )
        require_equal(response.status_code, 201, f"{action_name} should be registered")
    lease_response = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": agent["id"],
            "action": "repo_status",
            "intent": "Read repository status for implementation triage.",
            "ttl_seconds": 30,
        },
    )
    action_response = client.post(
        "/credential-actions",
        json={"lease_token": lease_response.json()["lease_token"], "action": "repo_summary", "payload": {"repo": "hivemind"}},
    )
    audit_events = client.get("/audit-events").json()

    require_equal(lease_response.status_code, 201, "lease should issue for the first tool action")
    require_equal(action_response.status_code, 403, "lease should not authorize another tool action")
    require_equal(action_response.json()["detail"], "credential lease does not allow this action", "lease should stay tool-action scoped")
    require_true(
        any(
            event["type"] == "credential.action.denied"
            and event["reason"] == "credential lease does not allow this action"
            and event["metadata"]["lease_id"] == lease_response.json()["id"]
            for event in audit_events
        ),
        "action mismatch should be audited as a denied brokered action",
    )


def test_tasks_and_schedules_reject_unregistered_tool_actions(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    agent = client.get("/agents").json()[0]
    credential = client.get("/credentials").json()[0]

    task_response = client.post(
        "/tasks",
        json={
            "title": "Unsafe task",
            "description": "Try an action outside the registry.",
            "priority": "normal",
            "assigned_agent_id": agent["id"],
            "credential_id": credential["id"],
            "action": "delete_repo",
            "intent": "Delete a repository without a registered action.",
            "heartbeat_seconds": None,
        },
    )
    schedule_response = client.post(
        "/schedules",
        json={
            "name": "Unsafe schedule",
            "enabled": True,
            "interval_seconds": 60,
            "task_title": "Unsafe scheduled task",
            "task_description": "Try an action outside the registry.",
            "priority": "normal",
            "assigned_agent_id": agent["id"],
            "credential_id": credential["id"],
            "action": "delete_repo",
            "intent": "Delete a repository without a registered action.",
            "next_run_at": None,
        },
    )

    require_equal(task_response.status_code, 400, "tasks should reject unknown registered actions")
    require_equal(task_response.json()["detail"], "action references unknown tool action: delete_repo", "task denial should name the unknown action")
    require_equal(schedule_response.status_code, 400, "schedules should reject unknown registered actions")
    require_equal(schedule_response.json()["detail"], "action references unknown tool action: delete_repo", "schedule denial should name the unknown action")


def test_credential_policy_rate_limits_are_exposed_and_enforced_for_agent_requests(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    agent = client.get("/agents").json()[0]

    response = client.post(
        "/credentials",
        json={
            "name": "Rate Limited GitHub",
            "provider": "github",
            "secret_ref": "env://RATE_LIMITED_GITHUB_TOKEN",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["read_repo"],
            "agent_lease_limit": 1,
            "rate_limit_window_seconds": 300,
            "provider_token_budget": 2000,
            "provider_cost_budget_cents": 75,
        },
    )
    require_equal(response.status_code, 201, "rate-limited credential should be created")
    credential = response.json()
    require_equal(credential["policy"]["agent_lease_limit"], 1, "agent lease limit should be exposed")
    require_equal(credential["policy"]["rate_limit_window_seconds"], 300, "rate window should be exposed")
    require_equal(credential["policy"]["provider_token_budget"], 2000, "token budget placeholder should be exposed")
    require_equal(credential["policy"]["provider_cost_budget_cents"], 75, "cost budget placeholder should be exposed")
    require_true("RATE_LIMITED_GITHUB_TOKEN" not in response.text, "credential response must redact secret refs")

    first = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": agent["id"],
            "action": "read_repo",
            "intent": "Read repository metadata for safe task triage.",
        },
    )
    second = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": agent["id"],
            "action": "read_repo",
            "intent": "Read repository metadata for follow-up triage.",
        },
    )

    require_equal(first.status_code, 201, "first lease should be issued")
    require_equal(second.status_code, 403, "agent lease rate limit should deny repeated requests")
    require_equal(second.json()["detail"], "agent lease request rate limit exceeded", "denial reason should name the limit")

    latest_audit = client.get("/audit-events").json()[0]
    require_equal(latest_audit["type"], "credential.lease.denied", "rate-limit denial should be audited")
    require_equal(latest_audit["metadata"]["rate_limit"], "agent_lease_limit", "audit should name the policy field")
    require_true("RATE_LIMITED_GITHUB_TOKEN" not in str(latest_audit), "rate-limit audit must not expose secret refs")


def test_credential_lease_limit_applies_across_agents(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    first_agent = client.get("/agents").json()[0]
    second_agent = client.post(
        "/agents",
        json={"name": "Builder", "role": "request a separate lease", "provider": "local"},
    ).json()
    credential = client.post(
        "/credentials",
        json={
            "name": "Credential Lease Limited",
            "provider": "github",
            "secret_ref": "env://CREDENTIAL_LEASE_LIMIT_TOKEN",
            "allowed_agents": [first_agent["id"], second_agent["id"]],
            "allowed_actions": ["read_repo"],
            "credential_lease_limit": 1,
            "rate_limit_window_seconds": 300,
        },
    ).json()

    first = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": first_agent["id"],
            "action": "read_repo",
            "intent": "Read repository metadata for safe task triage.",
        },
    )
    second = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": second_agent["id"],
            "action": "read_repo",
            "intent": "Read repository metadata for separate agent triage.",
        },
    )

    require_equal(first.status_code, 201, "first credential lease should be issued")
    require_equal(second.status_code, 403, "credential lease rate limit should apply across agents")
    require_equal(second.json()["detail"], "credential lease request rate limit exceeded", "denial should name credential limit")


def test_credential_lease_rate_limit_runs_before_provider_intent_review(tmp_path: Path) -> None:
    reviewer = RecordingProviderReviewer()
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
    setup_demo(client)
    agent = client.get("/agents").json()[0]
    credential = client.post(
        "/credentials",
        json={
            "name": "Provider Reviewed Limited Credential",
            "provider": "github",
            "secret_ref": "env://PROVIDER_REVIEW_LIMIT_TOKEN",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["read_repo"],
            "agent_lease_limit": 1,
            "rate_limit_window_seconds": 300,
        },
    ).json()

    first = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": agent["id"],
            "action": "read_repo",
            "intent": "Read repository metadata for safe provider-backed triage.",
        },
    )
    second = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": agent["id"],
            "action": "read_repo",
            "intent": "Read repository metadata for repeated provider-backed triage.",
        },
    )

    require_equal(first.status_code, 201, "first lease should pass provider-backed review")
    require_equal(second.status_code, 403, "second lease should be denied by rate limit")
    require_equal(len(reviewer.requests), 1, "rate-limited requests should not spend provider review calls")
    require_equal(second.json()["detail"], "agent lease request rate limit exceeded", "denial should name agent limit")


def test_credential_lease_rate_limit_counts_provider_review_denials(tmp_path: Path) -> None:
    reviewer = RecordingProviderReviewer(allowed=False, reason="provider reviewer denied the request")
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
    setup_demo(client)
    agent = client.get("/agents").json()[0]
    credential = client.post(
        "/credentials",
        json={
            "name": "Provider Denied Limited Credential",
            "provider": "github",
            "secret_ref": "env://PROVIDER_DENIED_LIMIT_TOKEN",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["read_repo"],
            "agent_lease_limit": 1,
            "rate_limit_window_seconds": 300,
        },
    ).json()

    first = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": agent["id"],
            "action": "read_repo",
            "intent": "Read repository metadata for provider-backed triage.",
        },
    )
    second = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": agent["id"],
            "action": "read_repo",
            "intent": "Read repository metadata for repeated provider-backed triage.",
        },
    )

    require_equal(first.status_code, 403, "first lease should be denied by provider review")
    require_equal(
        first.json()["detail"],
        "openrouter intent reviewer denied the request",
        "first denial should expose the provider decision reason",
    )
    require_equal(second.status_code, 403, "second lease should be denied by rate limit")
    require_equal(
        second.json()["detail"],
        "agent lease request rate limit exceeded",
        "second denial should avoid another provider review",
    )
    require_equal(len(reviewer.requests), 1, "provider-reviewed denials should count toward request limits")
    latest_audit = client.get("/audit-events").json()[0]
    require_equal(latest_audit["metadata"]["rate_limit"], "agent_lease_limit", "audit should name agent limit")
    require_true("PROVIDER_DENIED_LIMIT_TOKEN" not in str(latest_audit), "audit must not expose secret refs")


def test_credential_action_limit_denies_before_consuming_second_lease(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    agent = client.get("/agents").json()[0]
    credential = client.post(
        "/credentials",
        json={
            "name": "Action Limited GitHub",
            "provider": "github",
            "secret_ref": "env://ACTION_LIMITED_GITHUB_TOKEN",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["read_repo"],
            "credential_action_limit": 1,
            "rate_limit_window_seconds": 300,
        },
    ).json()
    first_lease = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": agent["id"],
            "action": "read_repo",
            "intent": "Read repository metadata for safe task triage.",
        },
    ).json()
    second_lease = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": agent["id"],
            "action": "read_repo",
            "intent": "Read repository metadata for follow-up triage.",
        },
    ).json()

    first_action = client.post(
        "/credential-actions",
        json={"lease_token": first_lease["lease_token"], "action": "read_repo", "payload": {"repo": "hivemind"}},
    )
    second_action = client.post(
        "/credential-actions",
        json={"lease_token": second_lease["lease_token"], "action": "read_repo", "payload": {"repo": "hivemind"}},
    )

    require_equal(first_action.status_code, 200, "first credential action should be allowed")
    require_equal(second_action.status_code, 403, "credential action limit should deny the second action")
    require_equal(second_action.json()["detail"], "credential action rate limit exceeded", "denial should name action limit")
    listed_second = next(item for item in client.get("/credential-leases").json() if item["id"] == second_lease["id"])
    require_equal(listed_second["status"], "active", "rate-limit-denied action should not consume the lease")

    latest_audit = client.get("/audit-events").json()[0]
    require_equal(latest_audit["type"], "credential.action.denied", "action rate-limit denial should be audited")
    require_equal(latest_audit["metadata"]["rate_limit"], "credential_action_limit", "audit should name action limit")
    require_true("ACTION_LIMITED_GITHUB_TOKEN" not in str(latest_audit), "action limit audit must not expose secret refs")


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
    setup_demo(client)
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
    setup_demo(client)
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
    setup_demo(client)
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

    setup_demo(client)
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
    setup_store_with_demo(setup_store)
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
    stores = [HivemindStore(db_path), HivemindStore(db_path)]

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
    setup_demo(client)
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


def test_registered_agent_provider_adapter_receives_model_and_brokered_credentials(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_MODEL", "anthropic/claude-sonnet-4")
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_ID", PROVIDER_CREDENTIAL_ID)
    adapter = RecordingAgentProviderAdapter("openrouter")
    store = HivemindStore(
        tmp_path / "hivemind.db",
        config=HivemindConfig.from_env(),
        agent_provider_adapters={"openrouter": adapter},
    )
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup_demo(client)
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
    create_provider_credential(store, agent["id"])
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
    audit_events = client.get("/audit-events").json()

    require_equal(response.status_code, 201, "registered provider adapter should execute the task")
    require_true(bool(adapter.requests), "registered adapter should receive a provider run request")
    provider_request = adapter.requests[0]
    require_equal(provider_request.provider, "openrouter", "adapter request should use the agent provider")
    require_equal(provider_request.model, "anthropic/claude-sonnet-4", "adapter request should use the agent model")
    require_equal(provider_request.system_prompt, "Keep output short.", "adapter request should include the agent system prompt")
    require_equal(provider_request.credential_id, PROVIDER_CREDENTIAL_ID, "adapter should receive the provider credential id")
    require_equal(
        provider_request.credential_action["credential_id"],
        PROVIDER_CREDENTIAL_ID,
        "adapter should receive a brokered provider credential action",
    )
    require_true(
        "lease_token" not in provider_request.credential_action,
        "adapter should not receive raw provider credential lease tokens",
    )
    require_equal(provider_request.tool_requests[0].name, "read_repo", "adapter should receive task tool requests")
    require_equal(
        provider_request.tool_requests[0].arguments["credential_id"],
        credential["id"],
        "tool request should reference credentials by id",
    )
    require_equal(
        provider_request.tool_requests[0].arguments["credential_action"]["credential_id"],
        credential["id"],
        "tool request should include a brokered credential action",
    )
    require_true(
        "lease_token" not in provider_request.tool_requests[0].arguments["credential_action"],
        "adapter should not receive raw tool credential lease tokens",
    )
    require_true(
        any(event["type"] == "task.execution.started" and event["target_id"] == task["id"] for event in audit_events),
        "task execution start should be audited",
    )
    require_true(
        any(
            event["type"] == "credential.action.performed"
            and event["target_id"] == PROVIDER_CREDENTIAL_ID
            and event["metadata"]["payload_key_count"] == 4
            for event in audit_events
        ),
        "provider credential use should consume a brokered credential action",
    )
    require_true(
        any(
            event["type"] == "credential.action.performed"
            and event["target_id"] == credential["id"]
            and event["metadata"]["payload_key_count"] == 4
            for event in audit_events
        ),
        "task tool credential use should consume a brokered credential action",
    )
    require_true("OPENROUTER_API_KEY" not in response.text, "task run response should not expose raw provider credential refs")
    require_true("credential_ref" not in response.json(), "task run response should omit provider credential refs")


def test_task_execution_redacts_legacy_secret_like_agent_prompts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_MODEL", "anthropic/claude-sonnet-4")
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_ID", PROVIDER_CREDENTIAL_ID)
    adapter = RecordingAgentProviderAdapter("openrouter")
    store = HivemindStore(
        tmp_path / "hivemind.db",
        config=HivemindConfig.from_env(),
        agent_provider_adapters={"openrouter": adapter},
    )
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup_demo(client)
    agent = client.post(
        "/agents",
        json={
            "name": "Legacy prompt runner",
            "role": "run a bounded provider-backed task",
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
            "system_prompt": "Keep output short.",
        },
    ).json()
    unsafe_prompt = "Use file:///var/lib/hivemind/provider-token with api_key = leaked."
    with store.connect() as conn:
        conn.execute("UPDATE agents SET system_prompt = ? WHERE id = ?", (unsafe_prompt, agent["id"]))
    create_provider_credential(store, agent["id"])
    task = client.post(
        "/tasks",
        json={
            "title": "Run with legacy prompt",
            "description": "Use the provider adapter boundary.",
            "assigned_agent_id": agent["id"],
        },
    ).json()

    response = client.post(f"/tasks/{task['id']}/run", json={})

    require_equal(response.status_code, 201, "registered provider adapter should execute the task")
    require_true(bool(adapter.requests), "registered adapter should receive a provider run request")
    provider_request = adapter.requests[0]
    require_equal(provider_request.system_prompt, "[redacted]", "adapter request should redact unsafe legacy prompts")
    require_true("provider-token" not in str(provider_request), "adapter request should not include secret refs")
    require_true("api_key = leaked" not in str(provider_request), "adapter request should not include secret assignments")


def test_agent_provider_credential_accepts_legacy_action_prefix(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_MODEL", "anthropic/claude-sonnet-4")
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_ID", PROVIDER_CREDENTIAL_ID)
    adapter = RecordingAgentProviderAdapter("openrouter")
    store = HivemindStore(
        tmp_path / "hivemind.db",
        config=HivemindConfig.from_env(),
        agent_provider_adapters={"openrouter": adapter},
    )
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup_demo(client)
    agent = client.post(
        "/agents",
        json={
            "name": "Legacy provider runner",
            "role": "run through an upgraded provider credential",
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
        },
    ).json()
    create_legacy_provider_credential(store, agent["id"])
    task = client.post(
        "/tasks",
        json={
            "title": "Run with legacy provider action",
            "description": "Existing provider credentials should remain usable after upgrade.",
            "assigned_agent_id": agent["id"],
        },
    ).json()

    response = client.post(f"/tasks/{task['id']}/run", json={})
    audit_events = client.get("/audit-events").json()

    require_equal(response.status_code, 201, "legacy provider credential action should still authorize")
    require_equal(len(adapter.requests), 1, "provider adapter should receive the authorized legacy credential request")
    provider_request = adapter.requests[0]
    require_equal(
        provider_request.credential_action["action"],
        f"{LEGACY_AGENT_PROVIDER_CREDENTIAL_ACTION_PREFIX}openrouter",
        "adapter should receive the exact legacy brokered action",
    )
    require_true(
        "lease_token" not in provider_request.credential_action,
        "legacy provider action should not expose raw lease tokens",
    )
    require_true(
        not any(
            event["type"] == "credential.lease.denied" and event["target_id"] == PROVIDER_CREDENTIAL_ID
            for event in audit_events
        ),
        "legacy provider credentials should not emit a denied lease before succeeding",
    )


def test_agent_provider_credential_policy_denial_fails_before_adapter(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_ID", PROVIDER_CREDENTIAL_ID)
    adapter = RecordingAgentProviderAdapter("openrouter")
    store = HivemindStore(
        tmp_path / "hivemind.db",
        config=HivemindConfig.from_env(),
        agent_provider_adapters={"openrouter": adapter},
    )
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup_demo(client)
    allowed_agent = client.get("/agents").json()[0]
    denied_agent = client.post(
        "/agents",
        json={
            "name": "Provider credential denied runner",
            "role": "should not receive provider credentials",
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
        },
    ).json()
    create_provider_credential(store, allowed_agent["id"])
    task = client.post(
        "/tasks",
        json={
            "title": "Denied provider credential use",
            "description": "The agent is outside the provider credential policy.",
            "assigned_agent_id": denied_agent["id"],
        },
    ).json()

    response = client.post(f"/tasks/{task['id']}/run", json={})
    updated_task = next(item for item in client.get("/tasks").json() if item["id"] == task["id"])
    audit_events = client.get("/audit-events").json()

    require_equal(response.status_code, 403, "provider credential policy denial should fail closed")
    require_equal(response.json()["detail"], "agent is not allowed to use this credential", "denial reason should match policy")
    require_equal(adapter.requests, [], "provider adapter should not receive policy-denied provider credential requests")
    require_equal(updated_task["status"], "failed", "policy-denied provider credential tasks should be marked failed")
    require_true(
        any(
            event["type"] == "credential.lease.denied"
            and event["target_id"] == PROVIDER_CREDENTIAL_ID
            and event["metadata"]["task_id"] == task["id"]
            and event["metadata"]["capability"] == "agent_provider"
            for event in audit_events
        ),
        "policy-denied provider credential requests should be audited",
    )


def test_agent_provider_task_credential_policy_denial_fails_before_adapter(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_ID", PROVIDER_CREDENTIAL_ID)
    adapter = RecordingAgentProviderAdapter("openrouter")
    store = HivemindStore(
        tmp_path / "hivemind.db",
        config=HivemindConfig.from_env(),
        agent_provider_adapters={"openrouter": adapter},
    )
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup_demo(client)
    denied_agent = client.post(
        "/agents",
        json={
            "name": "Denied provider runner",
            "role": "should not receive credential-scoped tool requests",
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
        },
    ).json()
    create_provider_credential(store, denied_agent["id"])
    credential = client.post(
        "/credentials",
        json={
            "name": "Repo reader",
            "provider": "github",
            "secret_ref": "env://GITHUB_TOKEN",
            "allowed_agents": [denied_agent["id"]],
            "allowed_actions": ["write_repo"],
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
    require_equal(response.json()["detail"], "action is outside this credential policy", "denial reason should match policy")
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
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_ID", PROVIDER_CREDENTIAL_ID)
    adapter = RecordingAgentProviderAdapter("openrouter")
    store = HivemindStore(
        tmp_path / "hivemind.db",
        config=HivemindConfig.from_env(),
        agent_provider_adapters={"openrouter": adapter},
    )
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup_demo(client)
    agent = client.post(
        "/agents",
        json={
            "name": "Approval gated provider runner",
            "role": "should not bypass operator approval",
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
        },
    ).json()
    create_provider_credential(store, agent["id"])
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
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_ID", PROVIDER_CREDENTIAL_ID)
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
    create_provider_credential(setup_store, agent["id"])
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
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_ID", PROVIDER_CREDENTIAL_ID)
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
    create_provider_credential(setup_store, agent["id"])
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
                "running",
                "agent should stay running while another assigned task is running",
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


def test_task_completion_preserves_manual_agent_status(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_ID", PROVIDER_CREDENTIAL_ID)
    db_path = tmp_path / "manual-agent-status.db"
    setup_store = HivemindStore(db_path, config=HivemindConfig.from_env())
    agent = setup_store.create_agent(
        {
            "name": "Provider runner",
            "role": "run a task while an operator updates status",
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
        }
    )
    create_provider_credential(setup_store, agent["id"])
    task = setup_store.create_task(
        {
            "title": "Blocked provider execution",
            "description": "This task pauses long enough for a manual lifecycle update.",
            "assigned_agent_id": agent["id"],
        }
    )
    adapter = BlockingAgentProviderAdapter(task["id"])
    runner = HivemindStore(db_path, config=HivemindConfig.from_env(), agent_provider_adapters={"openrouter": adapter})

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(runner.run_task, task["id"])
        adapter.started.wait(timeout=5)
        setup_store.update_agent_status(agent["id"], "blocked", actor_id="operator")
        adapter.release_slow.set()
        require_equal(result.result(timeout=5)["task_id"], task["id"], "task should finish after manual status update")

    require_equal(
        setup_store.get_agent(agent["id"])["status"],
        "blocked",
        "task completion should not overwrite a manual non-running lifecycle state",
    )


def test_remote_agent_provider_requires_credential_id_before_adapter_execution(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_ID", raising=False)
    adapter = RecordingAgentProviderAdapter("openrouter")
    store = HivemindStore(
        tmp_path / "hivemind.db",
        config=HivemindConfig.from_env(),
        agent_provider_adapters={"openrouter": adapter},
    )
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup_demo(client)
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

    require_equal(response.status_code, 403, "remote providers without credential ids should fail closed")
    require_true(
        "agent provider credential_id is not configured" in response.json()["detail"],
        "failure should explain that provider credentials are missing",
    )
    require_equal(adapter.requests, [], "provider adapter should not run without a configured credential id")
    require_equal(updated_task["status"], "failed", "credential configuration failures should mark the task failed")


def test_unregistered_agent_provider_fails_closed_without_leaking_secret_ref(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_ID", PROVIDER_CREDENTIAL_ID)
    store = HivemindStore(tmp_path / "hivemind.db", config=HivemindConfig.from_env())
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup_demo(client)
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
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_ID", PROVIDER_CREDENTIAL_ID)
    store = HivemindStore(
        tmp_path / "hivemind.db",
        config=HivemindConfig.from_env(),
        agent_provider_adapters={"openrouter": LeakingAgentProviderAdapter()},
    )
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup_demo(client)
    agent = client.post(
        "/agents",
        json={
            "name": "Leaky adapter runner",
            "role": "exercise provider redaction",
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
        },
    ).json()
    create_provider_credential(store, agent["id"])
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
    require_equal(
        result["tool_requests"][0]["arguments"]["credential_id"],
        PROVIDER_CREDENTIAL_ID,
        "credential ids can be returned without exposing secret refs",
    )
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
    require_equal(
        result["tool_requests"][0]["arguments"]["tuple_notes"],
        ["tuple ref env://SEC...", {"x-api-key": "[redacted]"}],
        "tuple provider payloads should be recursively redacted",
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
    monkeypatch.setenv("HIVEMIND_AGENT_PROVIDER_OPENROUTER_CREDENTIAL_ID", PROVIDER_CREDENTIAL_ID)
    store = HivemindStore(
        tmp_path / "hivemind.db",
        config=HivemindConfig.from_env(),
        agent_provider_adapters={"openrouter": LeakingAgentProviderAdapter(fail=True)},
    )
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup_demo(client)
    agent = client.post(
        "/agents",
        json={
            "name": "Failing adapter runner",
            "role": "exercise provider error redaction",
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
        },
    ).json()
    create_provider_credential(store, agent["id"])
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
    setup_demo(client)
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

    require_equal(response.status_code, 201, "GitHub App credential creation should succeed")
    credential = response.json()
    require_equal(credential["provider"], "github", "GitHub App credential should use the github provider")
    require_equal(credential["metadata"]["credential_kind"], "github_app", "credential kind should identify GitHub App")
    require_equal(credential["metadata"]["app_id"], "123456", "app id metadata should persist")
    require_equal(credential["metadata"]["installation_id"], "987654321", "installation id metadata should persist")
    require_equal(credential["policy"]["allowed_agents"], [agent["id"]], "GitHub App policy should preserve agent scope")
    require_equal(credential["policy"]["approval_required_actions"], [], "GitHub App policy should default to no approvals")
    require_true(credential["secret_ref_preview"].startswith("file://"), "public view should expose only the ref scheme")
    require_true("github-app.pem" not in response.text, "public response should redact the private key path")


def test_agent_registry_exposes_lifecycle_and_related_assignments(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)

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
    require_equal(create_response.status_code, 201, "agent creation should succeed")
    agent = create_response.json()
    require_equal(agent["status"], "idle", "new agents should start idle")
    require_equal(agent["assigned_task_count"], 0, "new agents should have no assigned tasks")
    require_equal(agent["assigned_schedule_count"], 0, "new agents should have no assigned schedules")
    require_equal(agent["credential_policy_count"], 0, "new agents should have no credential policies")
    require_equal(agent["assigned_tasks"], [], "new agents should have no task rollups")
    require_equal(agent["assigned_schedules"], [], "new agents should have no schedule rollups")
    require_equal(agent["credential_policies"], [], "new agents should have no credential policy rollups")

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
    require_equal(status_response.status_code, 200, "agent status update should succeed")
    updated = status_response.json()
    require_equal(updated["status"], "running", "status update should mark the agent running")
    require_equal(updated["assigned_task_count"], 1, "agent should report one assigned task")
    require_equal(updated["active_task_count"], 1, "running agent should report one active task")
    require_equal(updated["assigned_schedule_count"], 1, "agent should report one assigned schedule")
    require_equal(updated["credential_policy_count"], 1, "agent should report one credential policy")
    require_equal(updated["assigned_tasks"], [
        {
            "id": task["id"],
            "title": "Inspect repo state",
            "status": "queued",
            "priority": "normal",
            "updated_at": task["updated_at"],
        }
    ], "agent task rollups should include assigned task details")
    require_equal(updated["assigned_schedules"], [
        {
            "id": schedule["id"],
            "name": "Hourly repo scan",
            "enabled": True,
            "interval_seconds": 60,
            "next_run_at": schedule["next_run_at"],
            "task_title": "Scheduled repo scan",
        }
    ], "agent schedule rollups should include assigned schedule details")
    require_equal(updated["credential_policies"], [
        {
            "id": credential["id"],
            "name": "Scoped Repo Reader",
            "provider": "github",
            "allowed_actions": ["read_repo"],
            "max_ttl_seconds": 60,
            "require_intent": True,
        }
    ], "agent credential rollups should include scoped credential policy details")

    listed_agents = {item["id"]: item for item in client.get("/agents").json()}
    require_equal(listed_agents[agent["id"]]["status"], "running", "agent list should preserve updated status")
    require_equal(listed_agents[agent["id"]]["assigned_task_count"], 1, "agent list should include task rollup counts")


def test_unknown_agent_status_update_returns_404(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)

    response = client.patch("/agents/agent_missing/status", json={"status": "blocked"})

    require_equal(response.status_code, 404, "unknown agent status updates should return 404")
    require_equal(response.json()["detail"], "unknown agent: agent_missing", "unknown agent response should name the missing id")


def test_legacy_working_agent_status_alias_is_normalized(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)

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
    require_equal(create_response.status_code, 201, "agent creation should succeed before alias update")
    agent = create_response.json()

    response = client.patch(
        f"/agents/{agent['id']}/status",
        json={"status": "working"},
    )

    require_equal(response.status_code, 200, "legacy working status alias should be accepted")
    require_equal(response.json()["status"], "running", "legacy working status should normalize to running")


def test_agents_persist_across_store_restart(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)

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
    require_equal(create_response.status_code, 201, "agent creation should succeed before restart")
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
    setup_demo(client)

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

    require_equal(response.status_code, 400, "unknown credential agent should be rejected")
    require_equal(
        response.json()["detail"],
        "allowed_agents references unknown agent: agent_missing",
        "credential agent validation should name the missing id",
    )


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
    setup_demo(client)

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


def test_public_credential_metadata_redacts_secret_like_values(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
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


def test_credential_actions_accept_digits_after_first_character(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    agent = client.get("/agents").json()[0]

    response = client.post(
        "/credentials",
        json={
            "name": "GitHub Versioned Actions",
            "provider": "github",
            "secret_ref": "env://GITHUB_VERSIONED_ACTION_TOKEN",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["read_repo_v2", "oauth2_exchange"],
            "approval_required_actions": ["oauth2_exchange"],
            "max_ttl_seconds": 120,
            "require_intent": True,
            "metadata": {"credential_kind": "generic_reference"},
        },
    )

    require_equal(response.status_code, 201, "action names should allow digits after the first character")
    credential = response.json()
    require_equal(
        credential["policy"]["allowed_actions"],
        ["oauth2_exchange", "read_repo_v2"],
        "credential policy should preserve normalized versioned action names",
    )
    require_equal(
        credential["policy"]["approval_required_actions"],
        ["oauth2_exchange"],
        "approval policy should preserve normalized versioned action names",
    )


def test_approval_required_lease_flow_requires_operator_decision(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
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

    assert credential_response.status_code == 201  # nosec B101
    credential = credential_response.json()
    assert credential["policy"]["approval_required_actions"] == ["open_issue"]  # nosec B101

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

    assert pending_response.status_code == 201  # nosec B101
    pending_lease = pending_response.json()
    assert pending_lease["status"] == "pending"  # nosec B101
    assert pending_lease["token_preview"] == "not issued"  # nosec B101
    assert pending_lease["ttl_seconds"] == 180  # nosec B101
    assert "lease_token" not in pending_lease  # nosec B101

    listed_pending = client.get("/credential-leases").json()
    assert any(item["id"] == pending_lease["id"] and item["status"] == "pending" for item in listed_pending)  # nosec B101

    approve_response = client.post(f"/credential-leases/{pending_lease['id']}/approve")
    assert approve_response.status_code == 200  # nosec B101
    approved_lease = approve_response.json()
    assert approved_lease["status"] == "active"  # nosec B101
    assert approved_lease["ttl_seconds"] == 180  # nosec B101
    assert approved_lease["lease_token"].startswith("hvl_")  # nosec B101

    action_response = client.post(
        "/credential-actions",
        json={
            "lease_token": approved_lease["lease_token"],
            "action": "open_issue",
            "payload": {"repo": "hivemind", "title": "credential approval regression"},
        },
    )
    assert action_response.status_code == 200  # nosec B101
    assert action_response.json()["ok"] is True  # nosec B101

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
    assert deny_response.status_code == 200  # nosec B101
    denied_lease = deny_response.json()
    assert denied_lease["status"] == "denied"  # nosec B101
    assert denied_lease["token_preview"] == "not issued"  # nosec B101
    assert "lease_token" not in denied_lease  # nosec B101

    audit_events = client.get("/audit-events").json()
    assert any(  # nosec B101
        event["type"] == "credential.lease.pending"
        and event["metadata"]["lease_id"] == pending_lease["id"]
        and event["decision"] == "pending"
        for event in audit_events
    )
    assert any(  # nosec B101
        event["type"] == "credential.lease.approved"
        and event["metadata"]["lease_id"] == pending_lease["id"]
        and event["decision"] == "allowed"
        for event in audit_events
    )
    assert any(  # nosec B101
        event["type"] == "credential.lease.denied"
        and event["metadata"]["lease_id"] == denied_pending["id"]
        and event["reason"] == "operator denied lease request"
        for event in audit_events
    )


def test_hive_tracker_config_and_agent_issue_rate_limits(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)

    hive_response = client.post(
        "/hives",
        json={
            "name": "Linear Release Hive",
            "project_ref": "local://hivemind/release",
            "tracker_provider": "linear",
            "tracker_project": "HVM",
            "tracker_base_url": "https://linear.example.test",
            "guidance": "Queue only evidence-backed feature requests.",
        },
    )
    require_equal(hive_response.status_code, 201, "hive creation should succeed")
    hive = hive_response.json()
    require_equal(hive["tracker_provider"], "linear", "hive should store the tracker provider")
    require_equal(hive["tracker_project"], "HVM", "hive should store the tracker project")
    require_equal(hive["agent_count"], 0, "new hive should start without assigned agents")

    unassigned_issue_agent_response = client.post(
        "/agents",
        json={
            "name": "unassigned feature requester",
            "role": "Should not queue issues without a hive.",
            "provider": "local",
            "model": "deterministic-policy",
            "issue_creation_enabled": True,
            "issue_kind": "feature_request",
            "issue_rate_limit_per_hour": 1,
        },
    )
    require_equal(
        unassigned_issue_agent_response.status_code,
        400,
        "issue creation agents should require a hive assignment",
    )
    require_equal(
        unassigned_issue_agent_response.json()["detail"],
        "issue creation agents require hive_id",
        "missing hive denial should be explicit",
    )

    agent_response = client.post(
        "/agents",
        json={
            "name": "feature requester",
            "role": "Convert verified gaps into tracker-ready feature requests.",
            "provider": "local",
            "model": "deterministic-policy",
            "system_prompt": "Keep feature requests concise and evidence-backed.",
            "hive_id": hive["id"],
            "can_spawn_subagents": True,
            "max_subagents": 2,
            "issue_creation_enabled": True,
            "issue_kind": "feature_request",
            "issue_rate_limit_per_hour": 1,
            "issue_labels": ["feature", "needs-triage"],
        },
    )
    require_equal(agent_response.status_code, 201, "agent creation should succeed")
    agent = agent_response.json()
    require_equal(agent["hive_id"], hive["id"], "agent should be assigned to the hive")
    require_true(agent["can_spawn_subagents"], "agent should expose the subagent toggle")
    require_equal(agent["max_subagents"], 2, "agent should expose the subagent cap")
    require_true(agent["issue_creation_enabled"], "agent should expose issue creation toggle")
    require_equal(agent["issue_rate_limit_per_hour"], 1, "agent should expose issue rate")

    credential_response = client.post(
        "/credentials",
        json={
            "name": "Linear writer",
            "provider": "linear",
            "secret_ref": "env://LINEAR_API_TOKEN",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["open_feature_request"],
            "max_ttl_seconds": 120,
            "require_intent": True,
            "metadata": {"credential_kind": "generic_reference"},
        },
    )
    require_equal(credential_response.status_code, 201, "tracker credential should be stored as a reference")
    credential = credential_response.json()

    update_response = client.patch(
        f"/hives/{hive['id']}",
        json={"tracker_credential_id": credential["id"]},
    )
    require_equal(update_response.status_code, 200, "hive should accept tracker credential binding")
    require_equal(
        update_response.json()["tracker_credential_id"],
        credential["id"],
        "hive should expose the credential id without secret material",
    )

    request_response = client.post(
        "/issue-requests",
        json={
            "hive_id": hive["id"],
            "agent_id": agent["id"],
            "kind": "feature_request",
            "title": "Expose hive-level swarm activity",
            "description": "Operators need tracker-scoped visibility and rate-limited issue request agents.",
            "labels": ["operator"],
        },
    )
    require_equal(request_response.status_code, 201, "first issue request should be queued")
    task = request_response.json()
    require_equal(task["hive_id"], hive["id"], "issue request task should stay bound to the hive")
    require_equal(task["assigned_agent_id"], agent["id"], "issue request task should stay bound to the agent")
    require_equal(task["credential_id"], credential["id"], "issue request task should use the hive tracker credential")
    require_equal(task["action"], "open_feature_request", "feature request agents should queue the feature action")
    require_true(
        'Requested labels JSON: ["feature","needs-triage","operator"].' in task["intent"],
        "issue request task intent should persist merged labels for downstream execution",
    )

    limited_response = client.post(
        "/issue-requests",
        json={
            "hive_id": hive["id"],
            "agent_id": agent["id"],
            "kind": "feature_request",
            "title": "Second feature request inside the same hour",
        },
    )
    require_equal(limited_response.status_code, 429, "second request should hit the per-agent rate limit")
    require_equal(
        limited_response.json()["detail"],
        "agent issue request rate limit exceeded",
        "rate limit denial should be explicit",
    )

    refreshed_hive = next(item for item in client.get("/hives").json() if item["id"] == hive["id"])
    require_equal(refreshed_hive["agent_count"], 1, "hive should count assigned agents")
    require_equal(refreshed_hive["issue_agent_count"], 1, "hive should count issue-enabled agents")
    require_equal(refreshed_hive["subagent_enabled_count"], 1, "hive should count subagent-enabled agents")
    require_equal(refreshed_hive["open_task_count"], 1, "hive should count queued issue request tasks")

    audit_events = client.get("/audit-events").json()
    created_event = next(event for event in audit_events if event["type"] == "issue.request.created")
    denied_event = next(event for event in audit_events if event["type"] == "issue.request.denied")
    require_equal(created_event["actor_id"], agent["id"], "issue request audit should name the agent")
    require_equal(created_event["target_id"], hive["id"], "issue request audit should name the hive")
    require_equal(created_event["metadata"]["task_id"], task["id"], "issue request audit should link the task")
    require_equal(created_event["metadata"]["remaining_this_hour"], 0, "issue request audit should expose rate budget")
    require_equal(
        denied_event["reason"],
        "agent issue request rate limit exceeded",
        "rate-limited issue request should keep a denial audit row",
    )
    require_true("LINEAR_API_TOKEN" not in str(audit_events), "audit events must not expose tracker secret refs")


def test_approval_decision_audit_redacts_legacy_unsafe_action_identifier(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-unsafe-approval-action.db"
    store = HivemindStore(db_path)
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup_demo(client)
    agent = client.get("/agents").json()[0]
    credential_id = "cred_legacy_unsafe_action"
    unsafe_action = f"token-{secrets.token_hex(8)}"
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=60)
    lease_ids = ("lease_unsafe_pending_approve", "lease_unsafe_pending_deny")

    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO credentials
            (
              id, name, provider, secret_ref, allowed_agents, allowed_actions,
              approval_required_actions, max_ttl_seconds, require_intent,
              metadata, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                credential_id,
                "Legacy Unsafe Action",
                "github",
                "env://LEGACY_UNSAFE_ACTION_TOKEN",
                json.dumps([agent["id"]]),
                json.dumps([unsafe_action]),
                json.dumps([unsafe_action]),
                60,
                1,
                "{}",
                now.isoformat(),
                now.isoformat(),
            ),
        )
        for lease_id in lease_ids:
            lease_secret = f"hvp_{secrets.token_urlsafe(18)}"
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
                    lease_id,
                    store.hash_token(lease_secret),
                    "not issued",
                    credential_id,
                    agent["id"],
                    unsafe_action,
                    "Approve or deny a legacy pending lease without exposing its action value.",
                    60,
                    "pending",
                    now.isoformat(),
                    expires_at.isoformat(),
                ),
            )

    approve_response = client.post(f"/credential-leases/{lease_ids[0]}/approve")
    deny_response = client.post(f"/credential-leases/{lease_ids[1]}/deny")

    require_equal(approve_response.status_code, 200, "legacy pending lease approval should still work")
    require_equal(deny_response.status_code, 200, "legacy pending lease denial should still work")

    audit_events = client.get("/audit-events").json()
    approved_event = next(event for event in audit_events if event["type"] == "credential.lease.approved")
    denied_event = next(
        event
        for event in audit_events
        if event["type"] == "credential.lease.denied" and event["reason"] == "operator denied lease request"
    )

    require_equal(approved_event["metadata"]["action"], "<redacted>", "approval audit action should be redacted")
    require_equal(denied_event["metadata"]["action"], "<redacted>", "denial audit action should be redacted")
    require_true(unsafe_action not in str(audit_events), "unsafe action should not appear in audit events")


def test_persisted_pending_and_denied_lease_tokens_cannot_perform_actions(tmp_path: Path, caplog) -> None:
    db_path = tmp_path / "persisted-approval-status.db"
    store = HivemindStore(db_path)
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup_demo(client)
    caplog.set_level(logging.INFO, logger="hivemind.audit")
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
    audit_events = client.get("/audit-events").json()
    action_denials = [event for event in audit_events if event["type"] == "credential.action.denied"]
    require_equal(len(action_denials), 2, "denied credential actions should be audited")
    require_true(
        any(
            event["reason"] == "credential lease is pending approval"
            and event["metadata"]["lease_id"] == "lease_pending_known_hash"
            and event["metadata"]["lease_status"] == "pending"
            and "lease_token" not in event["metadata"]
            for event in action_denials
        ),
        "pending lease action denial should be audited without storing the token",
    )
    require_true(
        any(
            event["reason"] == "credential lease request was denied"
            and event["metadata"]["lease_id"] == "lease_denied_known_hash"
            and event["metadata"]["lease_status"] == "denied"
            and "lease_token" not in event["metadata"]
            for event in action_denials
        ),
        "denied lease action denial should be audited without storing the token",
    )
    require_true(lease_values["pending"] not in caplog.text, "pending lease token should not appear in structured logs")
    require_true(lease_values["denied"] not in caplog.text, "denied lease token should not appear in structured logs")


def test_operational_endpoints_return_401_before_auth(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    protected_requests = [
        ("GET", "/me", None),
        ("GET", "/config", None),
        ("GET", "/hives", None),
        (
            "POST",
            "/hives",
            {
                "name": "Release hive",
                "project_ref": "local://hivemind",
                "tracker_provider": "github",
                "tracker_project": "owner/repo",
            },
        ),
        ("PATCH", "/hives/hive_demo", {"status": "paused"}),
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
        ("GET", "/tool-actions", None),
        (
            "POST",
            "/tool-actions",
            {
                "name": "repo_metadata",
                "description": "Read repository metadata.",
                "input_schema": {"type": "object"},
                "required_credential_action": "read_repo",
                "risk_level": "low",
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
        (
            "POST",
            "/issue-requests",
            {
                "hive_id": "hive_demo",
                "agent_id": "agent_demo",
                "title": "Capture a verified feature request",
                "kind": "feature_request",
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
        ("GET", "/declarative-config", None),
        (
            "POST",
            "/declarative-config/validate",
            {"config": {"version": 1, "agents": [], "credentials": [], "schedules": []}},
        ),
        (
            "POST",
            "/declarative-config/import",
            {"dry_run": True, "config": {"version": 1, "agents": [], "credentials": [], "schedules": []}},
        ),
        ("GET", "/audit-events", None),
    ]

    for method, path, payload in protected_requests:
        response = client.request(method, path, json=payload)

        require_equal(response.status_code, 401, f"{method} {path} should require authentication")
        require_equal(response.json(), {"detail": "authentication required"}, f"{method} {path} should return a consistent auth error")


def test_create_credential_rejects_invalid_secret_ref(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)

    response = client.post(
        "/credentials",
        json={
            "name": "Bad Credential",
            "provider": "github",
            "secret_ref": "ghp_raw_secret_value",
            "allowed_actions": ["read_repo"],
        },
    )

    require_equal(response.status_code, 400, "invalid secret refs should be rejected")
    require_equal(
        response.json()["detail"],
        "secret_ref must use env://, file://, vault://, oauth://, or secret://",
        "invalid secret ref errors should list supported schemes",
    )


def test_create_credential_rejects_client_supplied_secret_ref(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)

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
    setup_demo(client)

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
    setup_demo(client)

    response = client.post(
        "/credentials",
        json={
            "name": "Broker Secret",
            "provider": "openrouter",
            "secret_value": "sk-test-local-secret",
            "allowed_actions": ["review_intent"],
        },
    )

    assert response.status_code == 400  # nosec B101
    assert response.json()["detail"] == "Set HIVEMIND_SECRETS_KEY to enable broker-side local secret storage."  # nosec B101
    assert "sk-test-local-secret" not in response.text  # nosec B101


def test_broker_managed_secret_is_encrypted_redacted_and_broker_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HIVEMIND_SECRETS_KEY", "local-test-secret-key")
    client = client_for(tmp_path)
    setup_demo(client)
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

    assert response.status_code == 201  # nosec B101
    credential = response.json()
    assert credential["provider"] == "openrouter"  # nosec B101
    assert credential["metadata"]["credential_kind"] == "managed_secret"  # nosec B101
    require_equal(credential["metadata"]["note"], "operator supplied", "managed secrets should preserve non-kind metadata")
    assert credential["secret_ref_preview"].startswith("secret://")  # nosec B101
    assert "secret_value" not in credential  # nosec B101
    assert managed_value not in response.text  # nosec B101

    list_response = client.get("/credentials")
    assert list_response.status_code == 200  # nosec B101
    assert managed_value not in list_response.text  # nosec B101

    store = client.app.state.store
    secret_box = SecretBox.from_env()
    assert secret_box is not None  # nosec B101
    assert store.resolve_broker_secret(credential["id"], secret_box) == managed_value  # nosec B101

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

    assert row is not None  # nosec B101
    assert row[0] == f"secret://{credential['id']}"  # nosec B101
    assert secret_row is not None  # nosec B101
    assert "BEGIN TEST SECRET" not in secret_row[0]  # nosec B101


def test_declarative_config_export_excludes_broker_managed_secret_refs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HIVEMIND_SECRETS_KEY", "local-test-secret-key")
    client = client_for(tmp_path)
    setup_demo(client)
    agent = client.get("/agents").json()[0]
    managed_value = secrets.token_urlsafe(16)

    managed_response = client.post(
        "/credentials",
        json={
            "name": "Broker Secret",
            "provider": "openrouter",
            "secret_value": managed_value,
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["read_repo"],
            "approval_required_actions": [],
            "max_ttl_seconds": 180,
            "require_intent": True,
            "metadata": {"note": "local only"},
        },
    )
    require_equal(managed_response.status_code, 201, "managed credential fixture should be created")
    managed_credential = managed_response.json()

    external_response = client.post(
        "/credentials",
        json={
            "name": "Portable Secret Ref",
            "provider": "github",
            "secret_ref": "env://HIVEMIND_PORTABLE_TOKEN",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["read_repo"],
            "approval_required_actions": [],
            "max_ttl_seconds": 180,
            "require_intent": True,
            "metadata": {"credential_kind": "generic_reference"},
        },
    )
    require_equal(external_response.status_code, 201, "external credential fixture should be created")
    external_credential = external_response.json()

    secret_box = SecretBox.from_env()
    if secret_box is None:
        raise AssertionError("OAuth credential fixture needs a broker secret box")
    oauth_access_token = secrets.token_urlsafe(16)
    oauth_refresh_token = secrets.token_urlsafe(16)
    oauth_credential = client.app.state.store.create_oauth_credential(
        provider="codex",
        token_payload={
            "access_token": oauth_access_token,
            "refresh_token": oauth_refresh_token,
            "scope": "read_repo",
            "expires_in": 1800,
        },
        requested_credential={
            "name": "OAuth Broker Credential",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["read_repo"],
            "approval_required_actions": [],
            "max_ttl_seconds": 180,
            "require_intent": True,
            "metadata": {},
        },
        secret_box=secret_box,
        actor_id="test-operator",
    )

    managed_schedule_response = client.post(
        "/schedules",
        json={
            "name": "Local secret schedule",
            "enabled": True,
            "interval_seconds": 120,
            "catch_up_policy": "skip_missed",
            "task_title": "Use local secret",
            "assigned_agent_id": agent["id"],
            "credential_id": managed_credential["id"],
            "action": "read_repo",
            "intent": "Use a local broker-managed credential only on this host.",
            "next_run_at": "2030-01-01T00:00:00+00:00",
        },
    )
    require_equal(managed_schedule_response.status_code, 201, "managed schedule fixture should be created")
    managed_schedule = managed_schedule_response.json()

    external_schedule_response = client.post(
        "/schedules",
        json={
            "name": "Portable schedule",
            "enabled": True,
            "interval_seconds": 120,
            "catch_up_policy": "skip_missed",
            "task_title": "Use portable ref",
            "assigned_agent_id": agent["id"],
            "credential_id": external_credential["id"],
            "action": "read_repo",
            "intent": "Use a portable external credential reference after import.",
            "next_run_at": "2030-01-01T00:00:00+00:00",
        },
    )
    require_equal(external_schedule_response.status_code, 201, "external schedule fixture should be created")
    external_schedule = external_schedule_response.json()

    oauth_schedule_response = client.post(
        "/schedules",
        json={
            "name": "OAuth-backed schedule",
            "enabled": True,
            "interval_seconds": 120,
            "catch_up_policy": "skip_missed",
            "task_title": "Use OAuth broker token",
            "assigned_agent_id": agent["id"],
            "credential_id": oauth_credential["id"],
            "action": "read_repo",
            "intent": "Use a broker-owned OAuth token only on this host.",
            "next_run_at": "2030-01-01T00:00:00+00:00",
        },
    )
    require_equal(oauth_schedule_response.status_code, 201, "OAuth schedule fixture should be created")
    oauth_schedule = oauth_schedule_response.json()

    export_response = client.get("/declarative-config")
    require_equal(export_response.status_code, 200, "declarative config export should succeed")
    exported = export_response.json()
    exported_credential_ids = {credential["id"] for credential in exported["credentials"]}
    exported_schedule_ids = {schedule["id"] for schedule in exported["schedules"]}

    require_true(
        managed_credential["id"] not in exported_credential_ids,
        "declarative export should omit broker-managed credentials",
    )
    require_true(
        managed_schedule["id"] not in exported_schedule_ids,
        "declarative export should omit schedules that depend on omitted credentials",
    )
    require_true(
        oauth_credential["id"] not in exported_credential_ids,
        "declarative export should omit OAuth-backed credentials",
    )
    require_true(
        oauth_schedule["id"] not in exported_schedule_ids,
        "declarative export should omit schedules that depend on OAuth-backed credentials",
    )
    require_true(external_credential["id"] in exported_credential_ids, "portable credentials should still export")
    require_true(external_schedule["id"] in exported_schedule_ids, "portable schedules should still export")
    require_true("secret://" not in export_response.text, "declarative export should not include broker refs")
    require_true("oauth://" not in export_response.text, "declarative export should not include OAuth refs")
    require_true(managed_value not in export_response.text, "declarative export should not include managed values")
    require_true(oauth_access_token not in export_response.text, "declarative export should not include OAuth access tokens")
    require_true(oauth_refresh_token not in export_response.text, "declarative export should not include OAuth refresh tokens")
    validate_response = client.post("/declarative-config/validate", json={"config": exported})
    require_equal(validate_response.status_code, 200, "declarative export should be self-validating")


def test_declarative_config_round_trips_without_raw_secrets(tmp_path: Path) -> None:
    source = client_for(tmp_path / "source")
    setup(source)

    agent_response = source.post(
        "/agents",
        json={
            "name": "Config Runner",
            "role": "Apply declarative runtime config.",
            "provider": "local",
            "model": "deterministic-policy",
            "system_prompt": "Report only actionable import failures.",
        },
    )
    require_equal(agent_response.status_code, 201, "agent import fixture should be created")
    agent = agent_response.json()

    credential_response = source.post(
        "/credentials",
        json={
            "name": "Config GitHub",
            "provider": "github",
            "secret_ref": "env://HIVEMIND_CONFIG_GITHUB_TOKEN",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["read_repo", "open_issue"],
            "approval_required_actions": ["open_issue"],
            "max_ttl_seconds": 180,
            "require_intent": True,
            "metadata": {
                "credential_kind": "generic_reference",
                "purpose": "config round trip",
                "operator_note": "operator pasted token=not-exported-marker",
                "fallback_ref": "env://NOT_EXPORTED_MARKER",
                "nested": {"client_secret": "not-exported-marker", "safe_note": "retained"},
                "camel": {"accessToken": "not-exported-marker", "safe_note": "retained"},
                "history": [
                    {"private_key": "not-exported-marker", "label": "kept"},
                    {"label": "also-kept", "note": "bearer not-exported-marker"},
                    "password:not-exported-marker",
                ],
            },
        },
    )
    require_equal(credential_response.status_code, 201, "credential import fixture should be created")
    credential = credential_response.json()

    schedule_response = source.post(
        "/schedules",
        json={
            "name": "Config sync",
            "enabled": True,
            "interval_seconds": 120,
            "catch_up_policy": "skip_missed",
            "task_title": "Validate imported config",
            "task_description": "Confirm references and policies still line up.",
            "priority": "high",
            "assigned_agent_id": agent["id"],
            "credential_id": credential["id"],
            "action": "read_repo",
            "intent": "Validate declarative Hivemind configuration drift.",
            "next_run_at": "2030-01-01T00:00:00+00:00",
        },
    )
    require_equal(schedule_response.status_code, 201, "schedule import fixture should be created")
    schedule = schedule_response.json()

    export_response = source.get("/declarative-config")
    require_equal(export_response.status_code, 200, "declarative config export should succeed")
    exported = export_response.json()
    exported_credential = next(item for item in exported["credentials"] if item["id"] == credential["id"])
    exported_schedule = next(item for item in exported["schedules"] if item["id"] == schedule["id"])

    require_equal(exported["version"], 1, "export should use the supported config version")
    require_equal(
        exported_credential["secret_ref"],
        "env://HIVEMIND_CONFIG_GITHUB_TOKEN",
        "export should keep secret refs as references",
    )
    require_true("secret_ref_preview" not in exported_credential, "declarative export should not use public previews")
    require_equal(
        exported_credential["metadata"]["nested"],
        {"safe_note": "retained"},
        "declarative export should recursively remove secret-like metadata keys",
    )
    require_equal(
        exported_credential["metadata"]["camel"],
        {"safe_note": "retained"},
        "declarative export should remove camelCase secret-like metadata keys",
    )
    require_equal(
        exported_credential["metadata"]["history"],
        [{"label": "kept"}, {"label": "also-kept"}],
        "declarative export should scrub secret-like metadata values inside lists",
    )
    require_true("operator_note" not in exported_credential["metadata"], "declarative export should omit secret-like metadata values")
    require_true("fallback_ref" not in exported_credential["metadata"], "declarative export should omit secret-ref metadata values")
    require_equal(
        exported_credential["policy"]["approval_required_actions"],
        ["open_issue"],
        "exported credential policy should preserve approval gates",
    )
    require_equal(
        exported_schedule["catch_up_policy"],
        "skip_missed",
        "exported schedule should preserve catch-up behavior",
    )
    require_true("task_template" in exported_schedule, "schedule export should include nested task template config")
    require_equal(
        exported_schedule["task_template"]["assigned_agent_id"],
        agent["id"],
        "task template should preserve agent reference",
    )
    require_true("not-exported-marker" not in export_response.text, "export should not include sensitive metadata")
    require_true("NOT_EXPORTED_MARKER" not in export_response.text, "export should not include sensitive metadata refs")

    target = client_for(tmp_path / "target")
    setup(target)

    validate_response = target.post("/declarative-config/validate", json={"config": exported})
    require_equal(validate_response.status_code, 200, "declarative config validation should succeed")
    require_equal(validate_response.json()["valid"], True, "validation response should mark config valid")

    dry_run_response = target.post(
        "/declarative-config/import",
        json={"dry_run": True, "config": exported},
    )
    require_equal(dry_run_response.status_code, 200, "dry-run import should succeed")
    require_equal(dry_run_response.json()["applied"], False, "dry-run import should not apply writes")
    require_true(
        all(item["id"] != agent["id"] for item in target.get("/agents").json()),
        "dry-run import should not create agents",
    )

    preexisting_config = deepcopy(exported)
    preexisting_credential = next(
        item for item in preexisting_config["credentials"] if item["id"] == credential["id"]
    )
    preexisting_credential["policy"]["approval_required_actions"] = []
    preexisting_import_response = target.post(
        "/declarative-config/import",
        json={"dry_run": False, "config": preexisting_config},
    )
    require_equal(
        preexisting_import_response.status_code,
        200,
        "preexisting credential fixture should import",
    )
    preexisting_imported_credential = next(
        item for item in target.get("/credentials").json() if item["id"] == credential["id"]
    )
    require_equal(
        preexisting_imported_credential["policy"]["approval_required_actions"],
        [],
        "preexisting credential fixture should start without approval gates",
    )

    import_response = target.post(
        "/declarative-config/import",
        json={"dry_run": False, "config": exported},
    )
    require_equal(import_response.status_code, 200, "declarative config import should succeed")
    require_equal(import_response.json()["applied"], True, "applied import should report writes")

    imported_agent = next(item for item in target.get("/agents").json() if item["id"] == agent["id"])
    imported_credential = next(item for item in target.get("/credentials").json() if item["id"] == credential["id"])
    imported_schedule = next(item for item in target.get("/schedules").json() if item["id"] == schedule["id"])
    audit_events = target.get("/audit-events").json()

    require_equal(imported_agent["name"], "Config Runner", "imported agent should preserve name")
    require_equal(imported_credential["secret_ref_preview"], "env://HIV...", "public credential view should stay redacted")
    require_equal(
        imported_credential["policy"]["allowed_agents"],
        [agent["id"]],
        "imported credential policy should preserve allowed agents",
    )
    require_equal(
        imported_credential["policy"]["approval_required_actions"],
        ["open_issue"],
        "imported credential policy should preserve approval gates",
    )
    require_equal(imported_schedule["task_title"], "Validate imported config", "imported schedule should keep task title")
    require_equal(imported_schedule["assigned_agent_id"], agent["id"], "imported schedule should keep agent reference")
    require_equal(
        imported_schedule["catch_up_policy"],
        "skip_missed",
        "imported schedule should keep catch-up behavior",
    )
    require_equal(audit_events[0]["type"], "config.imported", "applied import should create an audit event")
    require_true(audit_events[0]["actor_id"].startswith("user_"), "import audit should use the authenticated user")
    require_equal(
        audit_events[0]["metadata"]["credentials"],
        len(exported["credentials"]),
        "import audit should count credentials without naming secrets",
    )


def test_declarative_config_redacts_and_rejects_secret_like_agent_prompts(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    agent = client.get("/agents").json()[0]
    unsafe_prompt = "Use file:///var/lib/hivemind/provider-token with api_key = leaked."

    store = HivemindStore(tmp_path / "hivemind.db")
    with store.connect() as conn:
        conn.execute("UPDATE agents SET system_prompt = ? WHERE id = ?", (unsafe_prompt, agent["id"]))

    export_response = client.get("/declarative-config")
    require_equal(export_response.status_code, 200, "declarative export should succeed")
    exported_agent = next(item for item in export_response.json()["agents"] if item["id"] == agent["id"])
    require_equal(exported_agent["system_prompt"], "[redacted]", "declarative export should redact unsafe prompts")
    require_true("provider-token" not in export_response.text, "declarative export should not leak secret refs")
    require_true("api_key = leaked" not in export_response.text, "declarative export should not leak assignments")

    unsafe_config = {
        "version": 1,
        "agents": [
            {
                "id": "agent_unsafe_import",
                "name": "Unsafe import",
                "role": "Try to import prompt secrets.",
                "provider": "local",
                "model": "deterministic-policy",
                "system_prompt": unsafe_prompt,
            }
        ],
        "credentials": [],
        "schedules": [],
    }
    import_response = client.post(
        "/declarative-config/import",
        json={"dry_run": False, "config": unsafe_config},
    )
    require_equal(import_response.status_code, 400, "declarative import should reject unsafe prompts")
    require_true(
        "agents[0].system_prompt contains secret-like material" in import_response.json()["detail"],
        "declarative import error should identify the unsafe prompt field",
    )
    require_true("provider-token" not in import_response.text, "declarative import error should not echo secret refs")
    require_true(
        all(item["id"] != "agent_unsafe_import" for item in client.get("/agents").json()),
        "failed declarative import should not create unsafe agents",
    )


def test_declarative_config_normalizes_mixed_case_policy_actions(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    agent = client.get("/agents").json()[0]
    config = {
        "version": 1,
        "agents": [
            {
                "id": agent["id"],
                "name": agent["name"],
                "role": agent["role"],
                "provider": agent["provider"],
                "model": agent["model"],
                "system_prompt": agent["system_prompt"],
            }
        ],
        "credentials": [
            {
                "id": "cred_mixed_actions",
                "name": "Mixed Actions",
                "provider": "github",
                "secret_ref": "env://HIVEMIND_MIXED_ACTION_TOKEN",
                "policy": {
                    "allowed_agents": [agent["id"]],
                    "allowed_actions": ["Read_Repo", "Open_Issue"],
                    "approval_required_actions": ["Open_Issue"],
                    "max_ttl_seconds": 60,
                    "require_intent": True,
                },
                "metadata": {},
            }
        ],
        "schedules": [
            {
                "id": "sched_mixed_actions",
                "name": "Mixed case action",
                "enabled": True,
                "interval_seconds": 60,
                "catch_up_policy": "run_once",
                "next_run_at": "2030-01-01T00:00:00+00:00",
                "task_template": {
                    "title": "Read repo with mixed case action",
                    "assigned_agent_id": agent["id"],
                    "credential_id": "cred_mixed_actions",
                    "action": "Read_Repo",
                    "intent": "Read repository metadata with a normalized action.",
                },
            }
        ],
    }

    validate_response = client.post("/declarative-config/validate", json={"config": config})
    require_equal(validate_response.status_code, 200, "mixed-case action validation should succeed")
    import_response = client.post("/declarative-config/import", json={"dry_run": False, "config": config})
    require_equal(import_response.status_code, 200, "mixed-case action import should succeed")

    credential = next(item for item in client.get("/credentials").json() if item["id"] == "cred_mixed_actions")
    require_equal(
        credential["policy"]["allowed_actions"],
        ["open_issue", "read_repo"],
        "imported credential actions should be normalized",
    )
    require_equal(
        credential["policy"]["approval_required_actions"],
        ["open_issue"],
        "imported approval gates should be normalized",
    )


def test_declarative_config_accepts_existing_runtime_references(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    agent = client.get("/agents").json()[0]

    credential_response = client.post(
        "/credentials",
        json={
            "name": "Runtime credential",
            "provider": "github",
            "secret_ref": "env://HIVEMIND_RUNTIME_TOKEN",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["read_repo"],
            "approval_required_actions": [],
            "max_ttl_seconds": 120,
            "require_intent": True,
            "metadata": {},
        },
    )
    require_equal(credential_response.status_code, 201, "runtime credential fixture should be created")
    credential = credential_response.json()

    credential_update = {
        "version": 1,
        "agents": [],
        "credentials": [
            {
                "id": credential["id"],
                "name": "Runtime credential updated by config",
                "provider": "github",
                "secret_ref": "env://HIVEMIND_RUNTIME_TOKEN",
                "policy": {
                    "allowed_agents": [agent["id"]],
                    "allowed_actions": ["read_repo", "open_issue"],
                    "approval_required_actions": ["open_issue"],
                    "max_ttl_seconds": 240,
                    "require_intent": True,
                },
                "metadata": {},
            }
        ],
        "schedules": [],
    }
    validate_update_response = client.post("/declarative-config/validate", json={"config": credential_update})
    require_equal(validate_update_response.status_code, 200, "partial credential update should validate")
    import_update_response = client.post(
        "/declarative-config/import",
        json={"dry_run": False, "config": credential_update},
    )
    require_equal(import_update_response.status_code, 200, "partial credential update should import")
    updated_credential = next(item for item in client.get("/credentials").json() if item["id"] == credential["id"])
    require_equal(
        updated_credential["policy"]["approval_required_actions"],
        ["open_issue"],
        "partial credential import should update policy without redeclaring the agent",
    )

    schedule_import = {
        "version": 1,
        "agents": [],
        "credentials": [],
        "schedules": [
            {
                "id": "sched_runtime_refs",
                "name": "Runtime refs",
                "enabled": True,
                "interval_seconds": 120,
                "catch_up_policy": "run_once",
                "next_run_at": "2030-01-01T00:00:00+00:00",
                "task_template": {
                    "title": "Use existing runtime refs",
                    "assigned_agent_id": agent["id"],
                    "credential_id": credential["id"],
                    "action": "read_repo",
                    "intent": "Use existing runtime references in a partial config import.",
                },
            }
        ],
    }
    validate_schedule_response = client.post("/declarative-config/validate", json={"config": schedule_import})
    require_equal(validate_schedule_response.status_code, 200, "partial schedule import should validate")
    import_schedule_response = client.post(
        "/declarative-config/import",
        json={"dry_run": False, "config": schedule_import},
    )
    require_equal(import_schedule_response.status_code, 200, "partial schedule import should apply")

    default_time_import = deepcopy(schedule_import)
    default_time_schedule = default_time_import["schedules"][0]
    default_time_schedule["id"] = "sched_default_time"
    default_time_schedule["interval_seconds"] = 300
    default_time_schedule.pop("next_run_at")
    before_import = datetime.now(timezone.utc)
    default_time_response = client.post(
        "/declarative-config/import",
        json={"dry_run": False, "config": default_time_import},
    )
    after_import = datetime.now(timezone.utc)
    require_equal(default_time_response.status_code, 200, "missing next_run_at should use interval default")
    default_time_result = next(item for item in client.get("/schedules").json() if item["id"] == "sched_default_time")
    default_next_run_at = datetime.fromisoformat(default_time_result["next_run_at"])
    require_true(
        before_import + timedelta(seconds=300) <= default_next_run_at <= after_import + timedelta(seconds=300),
        "declarative schedule import should default next_run_at to the first interval boundary",
    )


def test_declarative_config_round_trips_long_schedule_intervals(tmp_path: Path) -> None:
    source = client_for(tmp_path / "source")
    setup(source)
    long_interval = 400 * 24 * 60 * 60

    schedule_response = source.post(
        "/schedules",
        json={
            "name": "Long interval schedule",
            "enabled": True,
            "interval_seconds": long_interval,
            "catch_up_policy": "run_once",
            "task_title": "Run long interval work",
            "task_description": "Check long-interval schedule config portability.",
            "priority": "normal",
            "action": "",
            "intent": "",
        },
    )
    require_equal(schedule_response.status_code, 201, "runtime schedule creation should accept long intervals")
    schedule_id = schedule_response.json()["id"]

    export_response = source.get("/declarative-config")
    require_equal(export_response.status_code, 200, "declarative config export should succeed")
    exported = export_response.json()
    exported_schedule = next(item for item in exported["schedules"] if item["id"] == schedule_id)
    require_equal(
        exported_schedule["interval_seconds"],
        long_interval,
        "declarative export should preserve runtime-valid long intervals",
    )

    validate_response = source.post("/declarative-config/validate", json={"config": exported})
    require_equal(validate_response.status_code, 200, "long-interval declarative export should validate")

    target = client_for(tmp_path / "target")
    setup(target)
    import_response = target.post(
        "/declarative-config/import",
        json={"dry_run": False, "config": exported},
    )
    require_equal(import_response.status_code, 200, "long-interval declarative export should import")
    imported_schedule = next(item for item in target.get("/schedules").json() if item["id"] == schedule_id)
    require_equal(
        imported_schedule["interval_seconds"],
        long_interval,
        "declarative import should preserve runtime-valid long intervals",
    )


def test_declarative_config_import_rejects_raw_secret_shapes(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    agent = client.get("/agents").json()[0]

    bad_secret_ref = {
        "version": 1,
        "agents": [
            {
                "id": agent["id"],
                "name": agent["name"],
                "role": agent["role"],
                "provider": agent["provider"],
                "model": agent["model"],
                "system_prompt": agent["system_prompt"],
            }
        ],
        "credentials": [
            {
                "id": "cred_bad",
                "name": "Bad config credential",
                "provider": "github",
                "secret_ref": "ghp_raw_secret_value",
                "policy": {
                    "allowed_agents": [agent["id"]],
                    "allowed_actions": ["read_repo"],
                    "approval_required_actions": [],
                    "max_ttl_seconds": 60,
                    "require_intent": True,
                },
                "metadata": {},
            }
        ],
        "schedules": [],
    }
    response = client.post(
        "/declarative-config/import",
        json={"dry_run": False, "config": bad_secret_ref},
    )
    require_equal(response.status_code, 400, "raw secret-shaped config import should fail")
    require_equal(
        response.json()["detail"],
        "credentials[0].secret_ref secret_ref must use env://, file://, vault://, oauth://, or secret://",
        "raw secret-shaped config import should explain the rejected secret ref",
    )
    require_true(
        all(item["id"] != "cred_bad" for item in client.get("/credentials").json()),
        "failed config import should not create credentials",
    )

    broker_secret_ref = {
        **bad_secret_ref,
        "credentials": [
            {
                **bad_secret_ref["credentials"][0],
                "secret_ref": "secret://cred_bad",
            }
        ],
    }
    broker_response = client.post(
        "/declarative-config/validate",
        json={"config": broker_secret_ref},
    )
    require_equal(broker_response.status_code, 400, "client-supplied broker secret ref should fail")
    require_equal(
        broker_response.json()["detail"],
        "credentials[0].secret_ref secret:// refs are broker-generated; provide secret_value for broker-managed storage",
        "declarative config should preserve broker-managed secret ref invariant",
    )

    oauth_secret_ref = {
        **bad_secret_ref,
        "credentials": [
            {
                **bad_secret_ref["credentials"][0],
                "secret_ref": "oauth://codex/cred_bad",
            }
        ],
    }
    oauth_response = client.post(
        "/declarative-config/validate",
        json={"config": oauth_secret_ref},
    )
    require_equal(oauth_response.status_code, 400, "client-supplied OAuth broker ref should fail")
    require_equal(
        oauth_response.json()["detail"],
        "credentials[0].secret_ref oauth:// refs are broker-generated; reconnect OAuth credentials after import",
        "declarative config should preserve OAuth broker credential boundaries",
    )

    bad_metadata = {
        **bad_secret_ref,
        "credentials": [
            {
                **bad_secret_ref["credentials"][0],
                "secret_ref": "env://HIVEMIND_CONFIG_GITHUB_TOKEN",
                "metadata": {"client_secret": "x"},
            }
        ],
    }
    metadata_response = client.post(
        "/declarative-config/validate",
        json={"config": bad_metadata},
    )
    require_equal(metadata_response.status_code, 400, "secret metadata validation should fail")
    require_equal(
        metadata_response.json()["detail"],
        "credentials[0].metadata.client_secret cannot contain secret material",
        "secret metadata validation should name the rejected key",
    )

    nested_metadata = {
        **bad_metadata,
        "credentials": [
            {
                **bad_metadata["credentials"][0],
                "metadata": {"safe": {"access_token": "x"}},
            }
        ],
    }
    nested_response = client.post(
        "/declarative-config/validate",
        json={"config": nested_metadata},
    )
    require_equal(nested_response.status_code, 400, "nested secret metadata validation should fail")
    require_equal(
        nested_response.json()["detail"],
        "credentials[0].metadata.safe.access_token cannot contain secret material",
        "nested secret metadata validation should name the rejected key path",
    )

    camel_metadata = {
        **bad_metadata,
        "credentials": [
            {
                **bad_metadata["credentials"][0],
                "metadata": {"safe": {"accessToken": "x"}},
            }
        ],
    }
    camel_response = client.post(
        "/declarative-config/validate",
        json={"config": camel_metadata},
    )
    require_equal(camel_response.status_code, 400, "camelCase secret metadata validation should fail")
    require_equal(
        camel_response.json()["detail"],
        "credentials[0].metadata.safe.accessToken cannot contain secret material",
        "camelCase secret metadata validation should name the rejected key path",
    )

    neutral_secret_value_metadata = {
        **bad_metadata,
        "credentials": [
            {
                **bad_metadata["credentials"][0],
                "metadata": {"operator_note": "pasted token=bad"},
            }
        ],
    }
    neutral_value_response = client.post(
        "/declarative-config/validate",
        json={"config": neutral_secret_value_metadata},
    )
    require_equal(neutral_value_response.status_code, 400, "secret-like metadata values should fail validation")
    require_equal(
        neutral_value_response.json()["detail"],
        "credentials[0].metadata.operator_note cannot contain secret material",
        "secret-like metadata value validation should name the rejected field",
    )

    managed_metadata = {
        **bad_metadata,
        "credentials": [
            {
                **bad_metadata["credentials"][0],
                "metadata": {"credential_kind": "managed_secret"},
            }
        ],
    }
    managed_response = client.post(
        "/declarative-config/validate",
        json={"config": managed_metadata},
    )
    require_equal(managed_response.status_code, 400, "managed secret metadata validation should fail")
    require_equal(
        managed_response.json()["detail"],
        "credentials[0].metadata managed_secret metadata is broker-generated; provide secret_value for broker-managed storage",
        "declarative config should reject forged managed secret metadata",
    )

    bad_schedule_policy = {
        **bad_secret_ref,
        "credentials": [
            {
                **bad_secret_ref["credentials"][0],
                "secret_ref": "env://HIVEMIND_CONFIG_GITHUB_TOKEN",
            }
        ],
        "schedules": [
            {
                "id": "sched_bad_policy",
                "name": "Bad policy schedule",
                "enabled": True,
                "interval_seconds": 60,
                "catch_up_policy": "run_once",
                "next_run_at": "2030-01-01T00:00:00+00:00",
                "task_template": {
                    "title": "Use disallowed action",
                    "assigned_agent_id": agent["id"],
                    "credential_id": "cred_bad",
                    "action": "delete_repo",
                    "intent": "Try a schedule action outside credential policy.",
                },
            }
        ],
    }
    policy_response = client.post(
        "/declarative-config/validate",
        json={"config": bad_schedule_policy},
    )
    require_equal(policy_response.status_code, 400, "schedule policy validation should fail")
    require_equal(
        policy_response.json()["detail"],
        "schedules[0].task_template.action is outside credential policy: delete_repo",
        "schedule policy validation should reject actions outside the credential policy",
    )

    schedule_without_catch_up = dict(bad_schedule_policy["schedules"][0])
    schedule_without_catch_up.pop("catch_up_policy")
    missing_catch_up_policy = {**bad_schedule_policy, "schedules": [schedule_without_catch_up]}
    missing_catch_up_response = client.post(
        "/declarative-config/validate",
        json={"config": missing_catch_up_policy},
    )
    require_equal(
        missing_catch_up_response.status_code,
        400,
        "schedule catch-up policy should be explicit",
    )
    require_equal(
        missing_catch_up_response.json()["detail"],
        "schedules[0].catch_up_policy must be a non-empty string",
        "declarative schedules should require explicit catch-up behavior",
    )

    bad_catch_up_policy = {
        **bad_schedule_policy,
        "schedules": [
            {
                **bad_schedule_policy["schedules"][0],
                "catch_up_policy": "drift_forever",
            }
        ],
    }
    bad_catch_up_response = client.post(
        "/declarative-config/validate",
        json={"config": bad_catch_up_policy},
    )
    require_equal(
        bad_catch_up_response.status_code,
        400,
        "invalid schedule catch-up policy should fail",
    )
    require_equal(
        bad_catch_up_response.json()["detail"],
        "schedules[0].catch_up_policy must be one of: skip_missed, run_once, backfill",
        "declarative schedules should validate catch-up behavior",
    )

    naive_schedule_time = {
        **bad_schedule_policy,
        "schedules": [
            {
                **bad_schedule_policy["schedules"][0],
                "next_run_at": "2030-01-01T00:00:00",
                "task_template": {
                    **bad_schedule_policy["schedules"][0]["task_template"],
                    "action": "read_repo",
                },
            }
        ],
    }
    naive_time_response = client.post(
        "/declarative-config/validate",
        json={"config": naive_schedule_time},
    )
    require_equal(
        naive_time_response.status_code,
        400,
        "naive schedule timestamps should fail",
    )
    require_equal(
        naive_time_response.json()["detail"],
        "schedules[0].next_run_at must include a timezone",
        "declarative schedules should fail closed before scheduler runtime",
    )

    missing_approval_gate = {
        **bad_secret_ref,
        "credentials": [
            {
                **bad_secret_ref["credentials"][0],
                "secret_ref": "env://HIVEMIND_CONFIG_GITHUB_TOKEN",
                "policy": {
                    "allowed_agents": [agent["id"]],
                    "allowed_actions": ["read_repo"],
                    "max_ttl_seconds": 60,
                    "require_intent": True,
                },
            }
        ],
    }
    missing_approval_response = client.post(
        "/declarative-config/validate",
        json={"config": missing_approval_gate},
    )
    require_equal(missing_approval_response.status_code, 400, "approval gate field should be explicit")
    require_equal(
        missing_approval_response.json()["detail"],
        "credentials[0].policy.approval_required_actions must be a list",
        "declarative credential policy should require explicit approval gate config",
    )

    bad_approval_gate = {
        **bad_secret_ref,
        "credentials": [
            {
                **bad_secret_ref["credentials"][0],
                "secret_ref": "env://HIVEMIND_CONFIG_GITHUB_TOKEN",
                "policy": {
                    **bad_secret_ref["credentials"][0]["policy"],
                    "approval_required_actions": ["delete_repo"],
                },
            }
        ],
    }
    approval_response = client.post(
        "/declarative-config/validate",
        json={"config": bad_approval_gate},
    )
    require_equal(approval_response.status_code, 400, "approval gate outside allowed actions should fail")
    require_equal(
        approval_response.json()["detail"],
        "credentials[0].policy.approval_required_actions is outside allowed_actions: delete_repo",
        "approval gate validation should reject actions outside the credential policy",
    )

    guided_metadata_missing_fields = {
        **bad_secret_ref,
        "credentials": [
            {
                **bad_secret_ref["credentials"][0],
                "provider": "github",
                "secret_ref": "env://HIVEMIND_CONFIG_GITHUB_TOKEN",
                "policy": {
                    "allowed_agents": [agent["id"]],
                    "allowed_actions": ["exchange_oauth_code"],
                    "approval_required_actions": [],
                    "max_ttl_seconds": 60,
                    "require_intent": True,
                },
                "metadata": {"credential_kind": "github_oauth_app"},
            }
        ],
        "schedules": [],
    }
    guided_validate_response = client.post(
        "/declarative-config/validate",
        json={"config": guided_metadata_missing_fields},
    )
    require_equal(
        guided_validate_response.status_code,
        400,
        "guided credential metadata should validate",
    )
    require_equal(
        guided_validate_response.json()["detail"],
        "credentials[0] github_oauth_app metadata requires client_id",
        "declarative validation should include store-level credential validation",
    )
    guided_import_response = client.post(
        "/declarative-config/import",
        json={"dry_run": False, "config": guided_metadata_missing_fields},
    )
    require_equal(
        guided_import_response.status_code,
        400,
        "guided credential import errors should be client errors",
    )
    require_equal(
        guided_import_response.json()["detail"],
        "credentials[0] github_oauth_app metadata requires client_id",
        "declarative import should map store validation errors to HTTP 400",
    )
    require_true(
        all(item["id"] != "cred_bad" for item in client.get("/credentials").json()),
        "failed guided metadata import should not create credentials",
    )


def test_guided_github_credential_metadata_is_validated(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)

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
    assert oauth_response.status_code == 400  # nosec B101
    assert oauth_response.json()["detail"] == "github_oauth_app metadata requires client_id"  # nosec B101

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
    assert app_response.status_code == 400  # nosec B101
    assert app_response.json()["detail"] == "github_app metadata requires app_id"  # nosec B101


def test_oauth_provider_status_reports_missing_broker_secret_store(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)

    response = client.get("/oauth/providers")

    assert response.status_code == 200  # nosec B101
    provider = response.json()[0]
    assert provider["id"] == "codex"  # nosec B101
    assert provider["available"] is False  # nosec B101
    assert "HIVEMIND_SECRETS_KEY" in provider["reason"]  # nosec B101


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
    setup_demo(client)
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

    assert start_response.status_code == 201  # nosec B101
    authorize_url = start_response.json()["authorize_url"]
    parsed = urlparse(authorize_url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"  # nosec B101
    assert parsed.netloc == "auth.example.test"  # nosec B101
    assert query["client_id"] == ["codex-client"]  # nosec B101
    assert query["scope"] == ["openid profile email offline_access"]  # nosec B101
    assert "state" in query  # nosec B101
    assert "code_challenge" in query  # nosec B101

    callback_response = client.get(
        f"/oauth/callback/codex?state={query['state'][0]}&code=broker-code",
        follow_redirects=False,
    )

    assert callback_response.status_code == 303  # nosec B101
    assert callback_response.headers["location"].startswith("/?oauth=connected")  # nosec B101

    credentials = client.get("/credentials").json()
    codex_credential = next(item for item in credentials if item["provider"] == "codex")
    assert codex_credential["name"] == "codex subscription"  # nosec B101
    assert codex_credential["secret_ref_preview"] == "oauth://cod..."  # nosec B101
    assert codex_credential["metadata"]["auth_type"] == "oauth"  # nosec B101
    assert codex_credential["metadata"]["oauth_refreshable"] is True  # nosec B101
    assert "access-secret-token" not in start_response.text  # nosec B101
    assert "access-secret-token" not in callback_response.text  # nosec B101
    assert "refresh-secret-token" not in callback_response.text  # nosec B101

    audit_events = client.get("/audit-events").json()
    assert audit_events[0]["type"] == "credential.oauth.connected"  # nosec B101

    conn = sqlite3.connect(tmp_path / "hivemind.db")
    token_row = conn.execute("SELECT token_ciphertext FROM oauth_connections").fetchone()
    conn.close()
    assert token_row is not None  # nosec B101
    assert "access-secret-token" not in token_row[0]  # nosec B101
    assert "refresh-secret-token" not in token_row[0]  # nosec B101
    assert captured["url"] == "https://auth.example.test/oauth/token"  # nosec B101
    assert captured["data"]["code"] == "broker-code"  # nosec B101
    assert captured["data"]["client_id"] == "codex-client"  # nosec B101
    assert captured["data"]["grant_type"] == "authorization_code"  # nosec B101
    assert "code_verifier" in captured["data"]  # nosec B101


def test_codex_oauth_start_rejects_invalid_actions_before_redirect(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HIVEMIND_SECRETS_KEY", "local-test-secret-key")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_AUTHORIZE_URL", "https://auth.example.test/oauth/authorize")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_TOKEN_URL", "https://auth.example.test/oauth/token")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_CLIENT_ID", "codex-client")

    client = client_for(tmp_path)
    setup_demo(client)
    agent = client.get("/agents").json()[0]

    response = client.post(
        "/oauth/credentials/start",
        json={
            "provider": "codex",
            "name": "codex subscription",
            "allowed_agents": [agent["id"]],
            "allowed_actions": ["repo-read"],
            "max_ttl_seconds": 900,
            "require_intent": True,
        },
    )

    require_equal(response.status_code, 400, "OAuth start should reject invalid actions before provider redirect")
    require_equal(
        response.json()["detail"],
        "actions must use lowercase snake_case names",
        "OAuth action validation should match credential creation",
    )
    conn = sqlite3.connect(tmp_path / "hivemind.db")
    oauth_state_count = conn.execute("SELECT COUNT(*) FROM oauth_states").fetchone()[0]
    conn.close()
    require_equal(oauth_state_count, 0, "invalid OAuth starts should not persist callback state")


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
    setup_demo(client)
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
    assert start_response.status_code == 201  # nosec B101
    authorize_url = start_response.json()["authorize_url"]
    query = parse_qs(urlparse(authorize_url).query)

    callback_response = client.get(
        f"/oauth/callback/codex?state={query['state'][0]}&code=broker-code",
        follow_redirects=False,
    )

    assert callback_response.status_code == 303  # nosec B101
    redirect_params = parse_qs(urlparse(callback_response.headers["location"]).query)
    assert redirect_params["oauth"] == ["error"]  # nosec B101
    assert redirect_params["detail"] == ["oauth token response must be a JSON object"]  # nosec B101
    audit_events = client.get("/audit-events").json()
    assert audit_events[0]["type"] == "credential.oauth.failed"  # nosec B101
    assert audit_events[0]["reason"] == "oauth token response must be a JSON object"  # nosec B101
    credentials = client.get("/credentials").json()
    assert all(item["provider"] != "codex" for item in credentials)  # nosec B101


def test_codex_oauth_flow_audits_unknown_state_callback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HIVEMIND_SECRETS_KEY", "local-test-secret-key")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_AUTHORIZE_URL", "https://auth.example.test/oauth/authorize")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_TOKEN_URL", "https://auth.example.test/oauth/token")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_CLIENT_ID", "codex-client")

    client = client_for(tmp_path)
    setup_demo(client)

    callback_response = client.get(
        "/oauth/callback/codex?state=oauth_state_missing&code=broker-code",
        follow_redirects=False,
    )

    assert callback_response.status_code == 303  # nosec B101
    redirect_params = parse_qs(urlparse(callback_response.headers["location"]).query)
    assert redirect_params["oauth"] == ["error"]  # nosec B101
    assert redirect_params["detail"] == ["unknown oauth state"]  # nosec B101
    audit_events = client.get("/audit-events").json()
    assert audit_events[0]["type"] == "credential.oauth.failed"  # nosec B101
    assert audit_events[0]["reason"] == "unknown oauth state"  # nosec B101
    assert audit_events[0]["target_id"] == "codex"  # nosec B101


def test_codex_oauth_flow_audits_missing_code_callback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HIVEMIND_SECRETS_KEY", "local-test-secret-key")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_AUTHORIZE_URL", "https://auth.example.test/oauth/authorize")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_TOKEN_URL", "https://auth.example.test/oauth/token")
    monkeypatch.setenv("HIVEMIND_OAUTH_CODEX_CLIENT_ID", "codex-client")

    client = client_for(tmp_path)
    setup_demo(client)
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
    assert start_response.status_code == 201  # nosec B101
    authorize_url = start_response.json()["authorize_url"]
    query = parse_qs(urlparse(authorize_url).query)

    callback_response = client.get(
        f"/oauth/callback/codex?state={query['state'][0]}",
        follow_redirects=False,
    )

    assert callback_response.status_code == 303  # nosec B101
    redirect_params = parse_qs(urlparse(callback_response.headers["location"]).query)
    assert redirect_params["oauth"] == ["error"]  # nosec B101
    assert redirect_params["detail"] == ["Missing OAuth authorization code."]  # nosec B101
    audit_events = client.get("/audit-events").json()
    assert audit_events[0]["type"] == "credential.oauth.failed"  # nosec B101
    assert audit_events[0]["reason"] == "Missing OAuth authorization code."  # nosec B101
    assert audit_events[0]["target_id"] == "codex"  # nosec B101
    credentials = client.get("/credentials").json()
    assert all(item["provider"] != "codex" for item in credentials)  # nosec B101


def test_tasks_heartbeats_and_due_schedules_run_once_by_default(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    me = client.get("/me").json()
    agent = client.get("/agents").json()[0]
    credential = client.get("/credentials").json()[0]
    base_now = datetime.now(timezone.utc).replace(microsecond=0)
    heartbeat_note = f"token={secrets.token_hex(8)}"

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
    assert task_response.status_code == 201  # nosec B101
    task = task_response.json()
    assert task["status"] == "queued"  # nosec B101
    assert task["priority"] == "urgent"  # nosec B101
    assert task["credential_id"] == credential["id"]  # nosec B101
    assert task["heartbeat_state"] == "healthy"  # nosec B101
    assert task["last_heartbeat_at"] is None  # nosec B101
    assert task["heartbeat_overdue_seconds"] is None  # nosec B101

    task_list = client.get("/tasks")
    assert task_list.status_code == 200  # nosec B101
    assert task_list.json()[0]["id"] == task["id"]  # nosec B101

    task_status = client.patch(f"/tasks/{task['id']}/status", json={"status": "blocked"})
    assert task_status.status_code == 200  # nosec B101
    assert task_status.json()["status"] == "blocked"  # nosec B101

    status_response = client.patch(f"/tasks/{task['id']}/status", json={"status": "running"})
    require_equal(status_response.status_code, 200, "task status update should succeed")

    heartbeat = client.post(f"/tasks/{task['id']}/heartbeats", json={"note": heartbeat_note})
    assert heartbeat.status_code == 201  # nosec B101
    heartbeats = client.get("/heartbeats").json()
    require_equal(heartbeats[0]["task_id"], task["id"], "heartbeat should be tied to the task")
    require_equal(heartbeats[0]["note"], heartbeat_note, "heartbeat history should retain the task-local note")
    tasks = {item["id"]: item for item in client.get("/tasks").json()}
    require_equal(tasks[task["id"]]["heartbeat_state"], "healthy", "heartbeat should keep task on cadence")
    require_equal(tasks[task["id"]]["last_heartbeat_at"], heartbeat.json()["created_at"], "task should expose last heartbeat")
    require_true(tasks[task["id"]]["next_heartbeat_at"] != task["next_heartbeat_at"], "heartbeat should advance next expected heartbeat")

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
    require_equal(schedule_run_event["actor_id"], me["id"], "manual schedule run should attribute the authenticated operator")
    require_equal(schedule_run_event["target_id"], schedule["id"], "schedule audit should target the schedule id")
    require_equal(schedule_run_event["decision"], "allowed", "schedule audit should record an allowed decision")
    require_equal(schedule_run_event["reason"], "scheduled task created", "schedule audit should describe the created task")
    audit_events = client.get("/audit-events")
    require_equal(audit_events.status_code, 200, "audit event listing should succeed")
    audit_event_list = audit_events.json()
    require_true(
        any(
            event["type"] == "task.created"
            and event["target_id"] == task["id"]
            and event["metadata"]["status"] == "queued"
            and event["metadata"]["priority"] == "urgent"
            and event["metadata"]["assigned_agent_id"] == agent["id"]
            and event["metadata"]["credential_id"] == credential["id"]
            for event in audit_event_list
        ),
        "task creation should be audited",
    )
    require_true(
        any(
            event["type"] == "task.status.updated"
            and event["target_id"] == task["id"]
            and event["metadata"] == {"from_status": "blocked", "to_status": "running"}
            for event in audit_event_list
        ),
        "task status update should be audited",
    )
    require_true(
        any(
            event["type"] == "task.heartbeat"
            and event["target_id"] == task["id"]
            and event["metadata"] == {"note_present": True, "note_length": len(heartbeat_note)}
            for event in audit_event_list
        ),
        "heartbeat audit should include only structured note metadata",
    )
    require_true(
        any(
            event["type"] == "schedule.created"
            and event["target_id"] == schedule["id"]
            and event["metadata"]["interval_seconds"] == 60
            and event["metadata"]["catch_up_policy"] == "run_once"
            and event["metadata"]["enabled"] is True
            for event in audit_event_list
        ),
        "schedule creation should be audited",
    )
    require_true(heartbeat_note not in str(audit_event_list), "raw heartbeat note should not appear in audit events")


def test_due_schedule_run_is_atomic_across_overlapping_store_instances(tmp_path: Path) -> None:
    db_path = tmp_path / "scheduler-race.db"
    creator = HivemindStore(db_path)
    base_now = datetime.now(timezone.utc).replace(microsecond=0)
    schedule = creator.create_schedule(
        {
            "name": "Overlapping run_once review",
            "interval_seconds": 60,
            "task_title": "One scheduled task",
            "next_run_at": (base_now - timedelta(seconds=120)).isoformat(),
        }
    )
    stores = [HivemindStore(db_path), HivemindStore(db_path)]
    start = Barrier(3)

    def run_due(store: HivemindStore) -> list[dict[str, object]]:
        start.wait()
        return store.run_due_schedules_once()

    lock_conn = sqlite3.connect(db_path)
    lock_released = False
    try:
        lock_conn.execute("BEGIN IMMEDIATE")
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run_due, store) for store in stores]
            start.wait()
            sleep(0.2)
            lock_conn.commit()
            lock_released = True
            results = [future.result(timeout=5) for future in futures]
    finally:
        if not lock_released:
            lock_conn.rollback()
        lock_conn.close()

    require_equal(
        sum(len(result) for result in results),
        1,
        "overlapping due schedule runners should create one run_once task",
    )
    with sqlite3.connect(db_path) as conn:
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        run_audit_count = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE type = 'schedule.ran' AND target_id = ?",
            (schedule["id"],),
        ).fetchone()[0]
    require_equal(task_count, 1, "overlapping runners should persist one task")
    require_equal(run_audit_count, 1, "overlapping runners should persist one schedule.ran audit event")


def test_due_schedule_run_rolls_back_partial_task_insert_on_failure(tmp_path: Path) -> None:
    class FailingScheduleAuditStore(HivemindStore):
        fail_schedule_audit = True

        def _insert_audit(self, conn, event_type, actor_id, target_id, decision, reason, metadata, *, now=None) -> None:
            if self.fail_schedule_audit and event_type == "schedule.ran":
                raise StoreError("forced schedule audit failure")
            super()._insert_audit(conn, event_type, actor_id, target_id, decision, reason, metadata, now=now)

    db_path = tmp_path / "scheduler-rollback.db"
    store = FailingScheduleAuditStore(db_path)
    due_at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=120)
    schedule = store.create_schedule(
        {
            "name": "Rollback review",
            "interval_seconds": 60,
            "task_title": "Rollback scheduled task",
            "next_run_at": due_at.isoformat(),
        }
    )

    try:
        store.run_due_schedules_once()
    except StoreError as exc:
        require_equal(str(exc), "forced schedule audit failure", "the injected schedule audit failure should surface")
    else:
        raise AssertionError("due schedule run should fail before commit")

    with sqlite3.connect(db_path) as conn:
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        task_audit_count = conn.execute("SELECT COUNT(*) FROM audit_events WHERE type = 'task.created'").fetchone()[0]
        persisted_schedule = conn.execute(
            "SELECT last_run_at, next_run_at FROM schedules WHERE id = ?",
            (schedule["id"],),
        ).fetchone()
    require_equal(task_count, 0, "failed due schedule transaction should not persist the task")
    require_equal(task_audit_count, 0, "failed due schedule transaction should not persist task audit")
    require_true(persisted_schedule[0] is None, "failed due schedule transaction should not set last_run_at")
    require_equal(
        persisted_schedule[1],
        due_at.isoformat(),
        "failed due schedule transaction should leave next_run_at unchanged",
    )

    store.fail_schedule_audit = False
    created = store.run_due_schedules_once()

    require_equal(len(created), 1, "rerunning after rollback should create the scheduled task once")
    with sqlite3.connect(db_path) as conn:
        task_count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    require_equal(task_count, 1, "successful rerun should persist one task")


def test_due_schedules_skip_missed_runs_and_preserve_cadence(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
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
    setup_demo(client)
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
    setup_demo(client)

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
    setup_demo(client)
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


def test_due_schedules_normalize_persisted_offset_next_run_at(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    offset_zone = timezone(timedelta(hours=14))
    due_at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=30)
    offset_due_at = due_at.astimezone(offset_zone)

    schedule_response = client.post(
        "/schedules",
        json={
            "name": "Offset persisted review",
            "interval_seconds": 60,
            "catch_up_policy": "backfill",
            "task_title": "Offset persisted scheduled review",
            "next_run_at": "2030-01-01T00:00:00+00:00",
        },
    )
    require_equal(schedule_response.status_code, 201, "schedule creation should succeed")
    schedule = schedule_response.json()

    with sqlite3.connect(tmp_path / "hivemind.db") as conn:
        conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", (offset_due_at.isoformat(), schedule["id"]))

    run_response = client.post("/schedules/run-due")

    require_equal(run_response.status_code, 200, "offset persisted due schedules should run")
    created_tasks = run_response.json()["created_tasks"]
    require_equal(len(created_tasks), 1, "offset persisted due schedules should create a task")
    require_equal(
        created_tasks[0]["title"],
        "Offset persisted scheduled review",
        "offset persisted schedule should create the configured task",
    )
    updated_schedule = next(item for item in client.get("/schedules").json() if item["id"] == schedule["id"])
    require_equal(
        datetime.fromisoformat(updated_schedule["next_run_at"]).tzinfo,
        timezone.utc,
        "running the offset schedule should normalize the next run timestamp to UTC",
    )
    metadata = latest_schedule_run_event(client, schedule["id"])["metadata"]
    require_equal(
        datetime.fromisoformat(metadata["scheduled_for"][0]),
        due_at,
        "schedule audit should record the absolute UTC slot, not the input offset string",
    )


def test_due_schedules_rejects_malformed_existing_next_run_at(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)

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


def test_health_reports_db_and_scheduler_state(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    response = client.get("/health")

    payload = response.json()
    require_equal(response.status_code, 200, "health should be available")
    require_equal(payload["status"], "ok", "health should report ok")
    require_equal(payload["db"]["status"], "ok", "health should report db ok")
    require_equal(payload["scheduler"]["status"], "disabled", "health should report disabled test scheduler")
    require_true(str(tmp_path / "hivemind.db") not in response.text, "health should not expose the database path")


def test_health_reports_scheduler_run_loop_failures(tmp_path: Path) -> None:
    store = HivemindStore(tmp_path / "scheduler-health.db")
    app = create_app(store, start_scheduler=False)

    class AliveThread:
        def is_alive(self) -> bool:
            return True

    with TestClient(app) as client:
        app.state.scheduler_enabled = True
        app.state.scheduler_thread = AliveThread()
        app.state.scheduler_last_error = "scheduled task run failed"

        response = client.get("/health")

    payload = response.json()
    require_equal(response.status_code, 200, "scheduler failures should keep health endpoint reachable")
    require_equal(payload["status"], "degraded", "scheduler failures should degrade runtime health")
    require_equal(payload["scheduler"]["status"], "error", "scheduler status should report run-loop failures")
    require_equal(payload["scheduler"]["last_error"], "scheduled task run failed", "scheduler failure should keep operator-safe detail")


def test_health_fails_clearly_when_db_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    store = HivemindStore(tmp_path / "health.db")
    app = create_app(store, start_scheduler=False)

    def broken_ping() -> None:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(store, "ping", broken_ping)

    with TestClient(app) as client:
        response = client.get("/health")

    payload = response.json()
    require_equal(response.status_code, 503, "health should report an unavailable database")
    require_equal(payload["status"], "error", "health status should fail")
    require_equal(payload["db"]["status"], "error", "db status should fail")
    require_equal(payload["db"]["detail"], "database unavailable", "db detail should be operator-safe")
    require_true("unable to open database file" not in response.text, "health response should not leak raw DB errors")


def test_runtime_overview_counts_active_leases_due_schedules_stale_heartbeats_and_failed_tasks(tmp_path: Path) -> None:
    store = HivemindStore(tmp_path / "runtime.db")
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup_demo(client)
    agent = client.get("/agents").json()[0]
    credential = client.get("/credentials").json()[0]

    lease_response = client.post(
        "/credential-leases",
        json={
            "credential_id": credential["id"],
            "agent_id": agent["id"],
            "action": "read_repo",
            "intent": "Inspect repository state for runtime health reporting.",
            "ttl_seconds": 300,
        },
    )
    require_equal(lease_response.status_code, 201, "lease creation should succeed")

    running_task = client.post(
        "/tasks",
        json={
            "title": "Heartbeat-bound task",
            "assigned_agent_id": agent["id"],
            "heartbeat_seconds": 60,
        },
    ).json()
    client.patch(f"/tasks/{running_task['id']}/status", json={"status": "running"})
    failed_task = client.post(
        "/tasks",
        json={
            "title": "Failed task",
            "assigned_agent_id": agent["id"],
        },
    ).json()
    client.patch(f"/tasks/{failed_task['id']}/status", json={"status": "failed"})
    schedule_response = client.post(
        "/schedules",
        json={
            "name": "Overdue review",
            "interval_seconds": 60,
            "task_title": "Scheduled review",
            "assigned_agent_id": agent["id"],
            "next_run_at": "2000-01-01T00:00:00+00:00",
        },
    )
    require_equal(schedule_response.status_code, 201, "schedule creation should succeed")
    schedule = schedule_response.json()

    with store.connect() as conn:
        conn.execute("UPDATE schedules SET next_run_at = ? WHERE id = ?", ("2000-01-01T00:00:00", schedule["id"]))
        conn.execute(
            "UPDATE tasks SET next_heartbeat_at = ?, updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00", "2000-01-01T00:00:00", running_task["id"]),
        )

    response = client.get("/runtime/overview")

    require_equal(response.status_code, 200, "runtime overview should be available")
    payload = response.json()
    require_equal(payload["status"], "ok", "runtime overview should report ok")
    require_equal(
        payload["counts"],
        {
            "active_leases": 1,
            "due_schedules": 1,
            "stale_heartbeats": 1,
            "failed_tasks": 1,
        },
        "runtime overview should count active and overdue work",
    )
    require_equal(payload["scheduler"]["status"], "disabled", "runtime overview should include scheduler status")
    require_equal(payload["due_schedule_ids"], [schedule["id"]], "runtime overview should include all due schedule ids")
    require_equal(payload["stale_heartbeat_task_ids"], [running_task["id"]], "runtime overview should include all stale task ids")
    require_equal(payload["due_schedules"][0]["name"], "Overdue review", "runtime overview should list overdue schedule")
    require_true(payload["due_schedules"][0]["overdue_seconds"] > 0, "due schedule should include overdue seconds")
    require_equal(payload["stale_heartbeats"][0]["id"], running_task["id"], "runtime overview should list stale heartbeat")
    require_true(payload["stale_heartbeats"][0]["overdue_seconds"] > 0, "stale heartbeat should include overdue seconds")
    require_equal(payload["failed_tasks"][0]["id"], failed_task["id"], "runtime overview should list failed task")


def test_runtime_overview_exposes_full_due_and_stale_id_sets(tmp_path: Path) -> None:
    store = HivemindStore(tmp_path / "runtime-ids.db")
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup_demo(client)
    agent = client.get("/agents").json()[0]
    due_schedule_ids = []
    stale_task_ids = []

    for index in range(2):
        task = client.post(
            "/tasks",
            json={
                "title": f"Heartbeat-bound task {index}",
                "assigned_agent_id": agent["id"],
                "heartbeat_seconds": 60,
            },
        ).json()
        client.patch(f"/tasks/{task['id']}/status", json={"status": "running"})
        stale_task_ids.append(task["id"])
        schedule = client.post(
            "/schedules",
            json={
                "name": f"Overdue review {index}",
                "interval_seconds": 60,
                "task_title": "Scheduled review",
                "assigned_agent_id": agent["id"],
                "next_run_at": f"2000-01-01T00:0{index}:00+00:00",
            },
        ).json()
        due_schedule_ids.append(schedule["id"])

    with store.connect() as conn:
        for task_id in stale_task_ids:
            conn.execute("UPDATE tasks SET next_heartbeat_at = ? WHERE id = ?", ("2000-01-01T00:00:00+00:00", task_id))

    overview = store.runtime_overview(limit=1)

    require_equal(overview["counts"]["due_schedules"], 2, "runtime overview should count all due schedules")
    require_equal(overview["counts"]["stale_heartbeats"], 2, "runtime overview should count all stale heartbeats")
    require_equal(len(overview["due_schedules"]), 1, "runtime detail list should still honor the display limit")
    require_equal(len(overview["stale_heartbeats"]), 1, "stale heartbeat detail list should still honor the display limit")
    require_equal(set(overview["due_schedule_ids"]), set(due_schedule_ids), "runtime overview should expose all due schedule ids")
    require_equal(set(overview["stale_heartbeat_task_ids"]), set(stale_task_ids), "runtime overview should expose all stale task ids")


def test_runtime_overview_uses_operator_safe_error_detail(tmp_path: Path, monkeypatch) -> None:
    store = HivemindStore(tmp_path / "runtime-error.db")
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup_demo(client)

    def broken_runtime_overview() -> dict[str, object]:
        raise sqlite3.OperationalError("raw database path /tmp/secret.db")

    monkeypatch.setattr(store, "runtime_overview", broken_runtime_overview)

    response = client.get("/runtime/overview")

    require_equal(response.status_code, 503, "runtime overview should report unavailable store state")
    require_equal(response.json()["detail"], "runtime overview unavailable", "runtime overview detail should be operator-safe")
    require_true("secret.db" not in response.text, "runtime overview response should not leak raw DB errors")


def test_audit_logs_are_structured_and_redact_sensitive_fields(tmp_path: Path, caplog) -> None:
    store = HivemindStore(tmp_path / "audit.db")
    caplog.set_level(logging.INFO, logger="hivemind.audit")

    store.audit(
        "credential.lease.issued",
        "agent_demo",
        "cred_demo",
        "allowed",
        "lease granted",
        {
            "action": "read_repo",
            "lease_token": "hvl_secret_token",
            "secret_ref": "env://demo-ref",
        },
    )

    records = [json.loads(record.getMessage()) for record in caplog.records if record.name == "hivemind.audit"]

    require_equal(records[-1]["event"], "audit.decision", "audit log should identify decision events")
    require_equal(records[-1]["type"], "credential.lease.issued", "audit log should include event type")
    require_equal(records[-1]["metadata"]["action"], "read_repo", "audit log should preserve non-secret action")
    require_equal(records[-1]["metadata"]["lease_token"], "[redacted]", "audit log should redact lease tokens")
    require_equal(records[-1]["metadata"]["secret_ref"], "[redacted]", "audit log should redact secret refs")
    require_true("hvl_secret_token" not in caplog.text, "audit log should not leak lease token values")
    require_true("demo-ref" not in caplog.text, "audit log should not leak secret ref values")
    stored_event = store.list_audit_events()[0]
    require_equal(stored_event["metadata"]["lease_token"], "[redacted]", "persisted audit event should redact lease tokens")
    require_equal(stored_event["metadata"]["secret_ref"], "[redacted]", "persisted audit event should redact secret refs")
    store.audit(
        "task.heartbeat",
        "agent_demo",
        "task_demo",
        "allowed",
        "heartbeat recorded",
        {"note": "operator pasted password=hunter2"},
    )
    records = [json.loads(record.getMessage()) for record in caplog.records if record.name == "hivemind.audit"]
    require_equal([record["type"] for record in records], ["credential.lease.issued"], "structured logs should stay scoped to broker decisions")
    require_true("hunter2" not in caplog.text, "runtime note content should not be emitted to structured broker logs")


def test_tasks_surface_missing_and_stale_heartbeats_and_clear_terminal_expectations(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    agent = client.get("/agents").json()[0]

    missing_task = client.post(
        "/tasks",
        json={
            "title": "Missing heartbeat",
            "assigned_agent_id": agent["id"],
            "heartbeat_seconds": 60,
        },
    ).json()
    stale_task = client.post(
        "/tasks",
        json={
            "title": "Stale heartbeat",
            "assigned_agent_id": agent["id"],
            "heartbeat_seconds": 60,
        },
    ).json()
    stale_heartbeat = client.post(
        f"/tasks/{stale_task['id']}/heartbeats",
        json={"note": "review in progress"},
    ).json()

    with sqlite3.connect(tmp_path / "hivemind.db") as conn:
        overdue_at = "2000-01-01T00:00:00+00:00"
        conn.execute("UPDATE tasks SET next_heartbeat_at = ? WHERE id = ?", (overdue_at, missing_task["id"]))
        conn.execute("UPDATE tasks SET next_heartbeat_at = ? WHERE id = ?", (overdue_at, stale_task["id"]))

    tasks = {item["id"]: item for item in client.get("/tasks").json()}
    require_equal(tasks[missing_task["id"]]["heartbeat_state"], "missing", "task without a first heartbeat should be marked missing")
    require_equal(tasks[missing_task["id"]]["last_heartbeat_at"], None, "missing task should not expose a heartbeat timestamp")
    require_true(tasks[missing_task["id"]]["heartbeat_overdue_seconds"] > 0, "missing task should expose overdue seconds")

    require_equal(tasks[stale_task["id"]]["heartbeat_state"], "stale", "task with an overdue heartbeat should be marked stale")
    require_equal(tasks[stale_task["id"]]["last_heartbeat_at"], stale_heartbeat["created_at"], "stale task should expose its latest heartbeat")
    require_true(tasks[stale_task["id"]]["heartbeat_overdue_seconds"] > 0, "stale task should expose overdue seconds")

    done_response = client.patch(f"/tasks/{stale_task['id']}/status", json={"status": "done"})
    require_equal(done_response.status_code, 200, "terminal task update should succeed")
    done_task = done_response.json()
    require_equal(done_task["heartbeat_state"], "disabled", "completed tasks should stop heartbeat tracking")
    require_equal(done_task["next_heartbeat_at"], None, "completed tasks should clear next heartbeat")
    require_equal(done_task["heartbeat_overdue_seconds"], None, "completed tasks should not report overdue heartbeats")
    terminal_heartbeat = client.post(
        f"/tasks/{stale_task['id']}/heartbeats",
        json={"note": "terminal follow-up"},
    )
    require_equal(terminal_heartbeat.status_code, 400, "terminal tasks should reject heartbeat notes")
    require_equal(
        terminal_heartbeat.json()["detail"],
        "cannot record heartbeat for task in terminal status: done",
        "terminal heartbeat rejection should explain the terminal task state",
    )
    terminal_task = next(item for item in client.get("/tasks").json() if item["id"] == stale_task["id"])
    require_equal(terminal_task["heartbeat_state"], "disabled", "terminal heartbeat rejection should keep tracking disabled")
    require_equal(terminal_task["next_heartbeat_at"], None, "terminal heartbeat rejection should not restore next heartbeat")
    require_equal(terminal_task["heartbeat_overdue_seconds"], None, "terminal heartbeat rejection should keep overdue state disabled")


def test_task_heartbeat_deadline_does_not_alert_before_due_second(tmp_path: Path) -> None:
    store = HivemindStore(tmp_path / "hivemind.db")
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup_demo(client)
    agent = client.get("/agents").json()[0]
    task_response = client.post(
        "/tasks",
        json={
            "title": "Heartbeat boundary",
            "assigned_agent_id": agent["id"],
            "heartbeat_seconds": 60,
        },
    )
    require_equal(task_response.status_code, 201, "boundary task should be created")
    task = task_response.json()
    stale_task_response = client.post(
        "/tasks",
        json={
            "title": "Stale heartbeat boundary",
            "assigned_agent_id": agent["id"],
            "heartbeat_seconds": 60,
        },
    )
    require_equal(stale_task_response.status_code, 201, "stale boundary task should be created")
    stale_task = stale_task_response.json()
    stale_heartbeat = client.post(f"/tasks/{stale_task['id']}/heartbeats", json={"note": "working"})
    require_equal(stale_heartbeat.status_code, 201, "stale boundary task should accept its initial heartbeat")
    deadline = datetime(2026, 1, 1, tzinfo=timezone.utc)

    with store.connect() as conn:
        conn.execute("UPDATE tasks SET next_heartbeat_at = ? WHERE id = ?", (deadline.isoformat(), task["id"]))
        conn.execute("UPDATE tasks SET next_heartbeat_at = ? WHERE id = ?", (deadline.isoformat(), stale_task["id"]))
        row = store.get_task_row(conn, task["id"])
        stale_row = store.get_task_row(conn, stale_task["id"])

        early = store.public_task(conn, row, now=deadline - timedelta(milliseconds=500))
        late = store.public_task(conn, row, now=deadline + timedelta(milliseconds=500))
        stale_early = store.public_task(conn, stale_row, now=deadline - timedelta(milliseconds=500))
        stale_late = store.public_task(conn, stale_row, now=deadline + timedelta(milliseconds=500))

    require_equal(early["heartbeat_state"], "healthy", "sub-second pre-deadline task should stay healthy")
    require_equal(early["heartbeat_overdue_seconds"], None, "sub-second pre-deadline task should not report overdue seconds")
    require_equal(late["heartbeat_state"], "missing", "sub-second post-deadline task should be overdue")
    require_equal(late["heartbeat_overdue_seconds"], 0, "sub-second post-deadline overdue value should round down")
    require_equal(stale_early["heartbeat_state"], "healthy", "sub-second pre-deadline heartbeat task should stay healthy")
    require_equal(stale_late["heartbeat_state"], "stale", "sub-second post-deadline heartbeat task should be stale")
    require_equal(stale_late["heartbeat_overdue_seconds"], 0, "sub-second post-deadline stale value should round down")


def test_task_management_flow_exposes_create_list_status_and_audit_state(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
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

    assert create_response.status_code == 201  # nosec B101
    created_task = create_response.json()
    assert created_task["title"] == "Review audit visibility"  # nosec B101
    assert created_task["description"] == "Verify task transitions remain visible in the operator API."  # nosec B101
    assert created_task["status"] == "queued"  # nosec B101
    assert created_task["priority"] == "urgent"  # nosec B101
    assert created_task["assigned_agent_id"] == agent["id"]  # nosec B101
    assert created_task["credential_id"] == credential["id"]  # nosec B101
    assert created_task["action"] == "read_repo"  # nosec B101
    assert created_task["intent"] == "Inspect the task-management API acceptance surface."  # nosec B101
    assert created_task["heartbeat_seconds"] == 90  # nosec B101
    assert created_task["next_heartbeat_at"] is not None  # nosec B101
    assert created_task["created_at"] == created_task["updated_at"]  # nosec B101

    list_response = client.get("/tasks")
    assert list_response.status_code == 200  # nosec B101
    assert list_response.json() == [created_task]  # nosec B101

    status_response = client.patch(f"/tasks/{created_task['id']}/status", json={"status": "running"})
    assert status_response.status_code == 200  # nosec B101
    updated_task = status_response.json()
    assert updated_task["id"] == created_task["id"]  # nosec B101
    assert updated_task["status"] == "running"  # nosec B101
    assert updated_task["updated_at"] != created_task["updated_at"]  # nosec B101
    assert updated_task["created_at"] == created_task["created_at"]  # nosec B101

    listed_after_update = client.get("/tasks")
    assert listed_after_update.status_code == 200  # nosec B101
    assert listed_after_update.json() == [updated_task]  # nosec B101

    heartbeat_response = client.post(
        f"/tasks/{created_task['id']}/heartbeats",
        json={"note": "operator verified the task is active"},
    )
    assert heartbeat_response.status_code == 201  # nosec B101
    heartbeat = heartbeat_response.json()
    assert heartbeat["task_id"] == created_task["id"]  # nosec B101
    assert heartbeat["agent_id"] == agent["id"]  # nosec B101
    assert heartbeat["note"] == "operator verified the task is active"  # nosec B101

    heartbeats_response = client.get(f"/heartbeats?task_id={created_task['id']}")
    assert heartbeats_response.status_code == 200  # nosec B101
    assert heartbeats_response.json() == [heartbeat]  # nosec B101

    audit_response = client.get("/audit-events")
    assert audit_response.status_code == 200  # nosec B101
    audit_events = audit_response.json()
    task_audit_events = [event for event in audit_events if event["target_id"] == created_task["id"]]

    assert [event["type"] for event in task_audit_events] == [  # nosec B101
        "task.heartbeat",
        "task.status.updated",
        "task.created",
    ]
    assert task_audit_events[0]["actor_id"] == me["id"]  # nosec B101
    assert task_audit_events[0]["decision"] == "allowed"  # nosec B101
    assert task_audit_events[0]["reason"] == "heartbeat recorded"  # nosec B101
    assert task_audit_events[0]["metadata"] == {  # nosec B101
        "note_present": True,
        "note_length": len("operator verified the task is active"),
    }
    assert task_audit_events[1]["actor_id"] == me["id"]  # nosec B101
    assert task_audit_events[1]["decision"] == "allowed"  # nosec B101
    assert task_audit_events[1]["reason"] == "task marked running"  # nosec B101
    assert task_audit_events[1]["metadata"] == {"from_status": "queued", "to_status": "running"}  # nosec B101
    assert task_audit_events[2]["actor_id"] == me["id"]  # nosec B101
    assert task_audit_events[2]["decision"] == "allowed"  # nosec B101
    assert task_audit_events[2]["reason"] == "task created"  # nosec B101
    assert task_audit_events[2]["metadata"] == {  # nosec B101
        "status": "queued",
        "priority": "urgent",
        "hive_id": agent["hive_id"],
        "assigned_agent_id": agent["id"],
        "credential_id": credential["id"],
        "action": "read_repo",
        "intent": "Inspect the task-management API acceptance surface.",
        "heartbeat_seconds": 90,
    }


def test_task_management_allows_non_status_updates_via_task_api(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
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

    assert update_response.status_code == 200  # nosec B101
    updated_task = update_response.json()
    assert updated_task["id"] == created_task["id"]  # nosec B101
    assert updated_task["title"] == "Updated task"  # nosec B101
    assert updated_task["description"] == "Updated description"  # nosec B101
    assert updated_task["status"] == "queued"  # nosec B101
    assert updated_task["priority"] == "high"  # nosec B101
    assert updated_task["assigned_agent_id"] is None  # nosec B101
    assert updated_task["credential_id"] is None  # nosec B101
    assert updated_task["action"] == "review_code"  # nosec B101
    assert updated_task["intent"] == "Verify the task update contract."  # nosec B101
    assert updated_task["heartbeat_seconds"] == 120  # nosec B101
    assert updated_task["next_heartbeat_at"] is not None  # nosec B101
    assert updated_task["created_at"] == created_task["created_at"]  # nosec B101
    assert updated_task["updated_at"] != created_task["updated_at"]  # nosec B101

    listed_tasks = client.get("/tasks")
    assert listed_tasks.status_code == 200  # nosec B101
    assert listed_tasks.json() == [updated_task]  # nosec B101


def test_task_update_rejects_null_title_and_clears_optional_text_fields(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)

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
    assert null_title_response.status_code == 400  # nosec B101
    assert null_title_response.json()["detail"] == "title must not be null"  # nosec B101

    cleared_response = client.patch(
        f"/tasks/{created_task['id']}",
        json={
            "description": None,
            "action": None,
            "intent": None,
            "heartbeat_seconds": None,
        },
    )
    assert cleared_response.status_code == 200  # nosec B101
    cleared_task = cleared_response.json()
    assert cleared_task["description"] == ""  # nosec B101
    assert cleared_task["action"] == ""  # nosec B101
    assert cleared_task["intent"] == ""  # nosec B101
    assert cleared_task["heartbeat_seconds"] is None  # nosec B101
    assert cleared_task["next_heartbeat_at"] is None  # nosec B101


def test_task_update_rejects_payloads_without_editable_fields(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)

    created_task = client.post(
        "/tasks",
        json={"title": "Strict update task"},
    ).json()

    empty_response = client.patch(
        f"/tasks/{created_task['id']}",
        json={},
    )
    require_equal(empty_response.status_code, 400, "empty task updates should be rejected")
    require_equal(
        empty_response.json()["detail"],
        "task update requires at least one editable field",
        "empty task update errors should identify the missing editable fields",
    )

    wrong_endpoint_response = client.patch(
        f"/tasks/{created_task['id']}",
        json={"status": "running"},
    )
    require_equal(wrong_endpoint_response.status_code, 422, "status-only task detail updates should fail validation")

    mixed_payload_response = client.patch(
        f"/tasks/{created_task['id']}",
        json={"title": "Ambiguous task update", "status": "done"},
    )
    require_equal(mixed_payload_response.status_code, 422, "mixed task detail/status updates should fail validation")

    unchanged_task = client.get("/tasks").json()[0]
    require_equal(unchanged_task["title"], "Strict update task", "invalid task updates should leave title unchanged")
    require_equal(unchanged_task["status"], "queued", "invalid task updates should leave status unchanged")


def test_task_management_state_persists_across_app_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "task-persistence.db"
    first_client = TestClient(create_app(HivemindStore(db_path), start_scheduler=False), base_url="https://testserver")
    setup_demo(first_client)
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
    assert create_response.status_code == 201  # nosec B101
    created_task = create_response.json()

    status_response = first_client.patch(f"/tasks/{created_task['id']}/status", json={"status": "blocked"})
    assert status_response.status_code == 200  # nosec B101

    heartbeat_response = first_client.post(
        f"/tasks/{created_task['id']}/heartbeats",
        json={"note": "restart-safe heartbeat"},
    )
    assert heartbeat_response.status_code == 201  # nosec B101
    recorded_heartbeat = heartbeat_response.json()
    first_client.close()

    second_client = TestClient(create_app(HivemindStore(db_path), start_scheduler=False), base_url="https://testserver")
    login_response = second_client.post(
        "/auth/login",
        json={"username": "admin", "password": TEST_PASSWORD},
    )
    assert login_response.status_code == 200  # nosec B101

    tasks_response = second_client.get("/tasks")
    assert tasks_response.status_code == 200  # nosec B101
    persisted_task = tasks_response.json()[0]
    assert persisted_task["id"] == created_task["id"]  # nosec B101
    assert persisted_task["title"] == "Persist queued task"  # nosec B101
    assert persisted_task["status"] == "blocked"  # nosec B101
    assert persisted_task["assigned_agent_id"] == agent["id"]  # nosec B101
    assert persisted_task["credential_id"] == credential["id"]  # nosec B101
    assert persisted_task["action"] == "read_repo"  # nosec B101
    assert persisted_task["intent"] == "Carry the task contract through a restart."  # nosec B101
    assert persisted_task["heartbeat_seconds"] == 60  # nosec B101
    assert persisted_task["next_heartbeat_at"] is not None  # nosec B101

    heartbeats_response = second_client.get(f"/heartbeats?task_id={created_task['id']}")
    assert heartbeats_response.status_code == 200  # nosec B101
    assert heartbeats_response.json() == [recorded_heartbeat]  # nosec B101

    audit_response = second_client.get("/audit-events")
    assert audit_response.status_code == 200  # nosec B101
    persisted_task_events = [event for event in audit_response.json() if event["target_id"] == created_task["id"]]
    assert [event["type"] for event in persisted_task_events] == [  # nosec B101
        "task.heartbeat",
        "task.status.updated",
        "task.created",
    ]
    assert persisted_task_events[0]["actor_id"] == first_user["id"]  # nosec B101
    assert persisted_task_events[0]["metadata"] == {  # nosec B101
        "note_present": True,
        "note_length": len("restart-safe heartbeat"),
    }
    assert persisted_task_events[1]["actor_id"] == first_user["id"]  # nosec B101
    assert persisted_task_events[1]["reason"] == "task marked blocked"  # nosec B101
    assert persisted_task_events[1]["metadata"] == {"from_status": "queued", "to_status": "blocked"}  # nosec B101
    assert persisted_task_events[2]["actor_id"] == first_user["id"]  # nosec B101
    assert persisted_task_events[2]["metadata"] == {  # nosec B101
        "status": "queued",
        "priority": "normal",
        "hive_id": agent["hive_id"],
        "assigned_agent_id": agent["id"],
        "credential_id": credential["id"],
        "action": "read_repo",
        "intent": "Carry the task contract through a restart.",
        "heartbeat_seconds": 60,
    }
    second_client.close()


def test_task_status_transitions_and_terminal_heartbeats_are_enforced(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    agent = client.get("/agents").json()[0]

    invalid_create = client.post(
        "/tasks",
        json={
            "title": "Invalid terminal start",
            "status": "done",
        },
    )
    assert invalid_create.status_code == 400  # nosec B101
    assert invalid_create.json()["detail"] == "new tasks must start in one of: blocked, queued, running"  # nosec B101

    task = client.post(
        "/tasks",
        json={
            "title": "Transition guard",
            "assigned_agent_id": agent["id"],
            "heartbeat_seconds": 60,
        },
    ).json()

    done_response = client.patch(f"/tasks/{task['id']}/status", json={"status": "done"})
    assert done_response.status_code == 200  # nosec B101
    assert done_response.json()["status"] == "done"  # nosec B101
    assert done_response.json()["next_heartbeat_at"] is None  # nosec B101

    invalid_transition = client.patch(f"/tasks/{task['id']}/status", json={"status": "running"})
    assert invalid_transition.status_code == 400  # nosec B101
    assert invalid_transition.json()["detail"] == "cannot transition task from done to running"  # nosec B101

    terminal_heartbeat = client.post(
        f"/tasks/{task['id']}/heartbeats",
        json={"note": "should be rejected"},
    )
    assert terminal_heartbeat.status_code == 400  # nosec B101
    assert terminal_heartbeat.json()["detail"] == "cannot record heartbeat for task in terminal status: done"  # nosec B101

    blocked_task = client.post(
        "/tasks",
        json={
            "title": "Blocked transition guard",
            "status": "blocked",
            "assigned_agent_id": agent["id"],
        },
    ).json()
    blocked_to_queued = client.patch(f"/tasks/{blocked_task['id']}/status", json={"status": "queued"})
    assert blocked_to_queued.status_code == 200  # nosec B101
    assert blocked_to_queued.json()["status"] == "queued"  # nosec B101
    queued_to_running = client.patch(f"/tasks/{blocked_task['id']}/status", json={"status": "running"})
    assert queued_to_running.status_code == 200  # nosec B101
    assert queued_to_running.json()["status"] == "running"  # nosec B101

    failed_task = client.post(
        "/tasks",
        json={
            "title": "Failed transition guard",
            "assigned_agent_id": agent["id"],
            "heartbeat_seconds": 60,
        },
    ).json()
    failed_response = client.patch(f"/tasks/{failed_task['id']}/status", json={"status": "failed"})
    assert failed_response.status_code == 200  # nosec B101
    assert failed_response.json()["status"] == "failed"  # nosec B101
    assert failed_response.json()["next_heartbeat_at"] is None  # nosec B101
    failed_heartbeat = client.post(
        f"/tasks/{failed_task['id']}/heartbeats",
        json={"note": "should be rejected"},
    )
    assert failed_heartbeat.status_code == 400  # nosec B101
    assert failed_heartbeat.json()["detail"] == "cannot record heartbeat for task in terminal status: failed"  # nosec B101

    cancelled_task = client.post(
        "/tasks",
        json={
            "title": "Cancelled transition guard",
            "assigned_agent_id": agent["id"],
            "heartbeat_seconds": 60,
        },
    ).json()
    running_response = client.patch(f"/tasks/{cancelled_task['id']}/status", json={"status": "running"})
    assert running_response.status_code == 200  # nosec B101
    cancelled_response = client.patch(f"/tasks/{cancelled_task['id']}/status", json={"status": "cancelled"})
    assert cancelled_response.status_code == 200  # nosec B101
    assert cancelled_response.json()["status"] == "cancelled"  # nosec B101
    assert cancelled_response.json()["next_heartbeat_at"] is None  # nosec B101
    cancelled_heartbeat = client.post(
        f"/tasks/{cancelled_task['id']}/heartbeats",
        json={"note": "should be rejected"},
    )
    assert cancelled_heartbeat.status_code == 400  # nosec B101
    assert cancelled_heartbeat.json()["detail"] == "cannot record heartbeat for task in terminal status: cancelled"  # nosec B101

    edited_done_task = client.patch(
        f"/tasks/{task['id']}",
        json={"heartbeat_seconds": 120},
    )
    assert edited_done_task.status_code == 200  # nosec B101
    assert edited_done_task.json()["heartbeat_seconds"] == 120  # nosec B101
    assert edited_done_task.json()["next_heartbeat_at"] is None  # nosec B101


def test_terminal_task_heartbeat_deadlines_are_cleared_during_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "terminal-heartbeat-migration.db"
    store = HivemindStore(db_path)
    created_at = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    stale_at = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc).isoformat()
    rows = [
        ("task_done", "done task", "done", stale_at, created_at, created_at),
        ("task_failed", "failed task", "failed", stale_at, created_at, created_at),
        ("task_cancelled", "cancelled task", "cancelled", stale_at, created_at, created_at),
        ("task_queued", "queued task", "queued", stale_at, created_at, created_at),
    ]
    with store.connect() as conn:
        conn.executemany(
            """
            INSERT INTO tasks
            (id, title, description, status, priority, assigned_agent_id, credential_id, action, intent, heartbeat_seconds, next_heartbeat_at, created_at, updated_at)
            VALUES (?, ?, '', ?, 'normal', NULL, NULL, '', '', 60, ?, ?, ?)
            """,
            rows,
        )

    HivemindStore(db_path)

    with store.connect() as conn:
        migrated_rows = {
            row["id"]: dict(row)
            for row in conn.execute("SELECT id, next_heartbeat_at, updated_at FROM tasks")
        }

    assert migrated_rows["task_done"]["next_heartbeat_at"] is None  # nosec B101
    assert migrated_rows["task_failed"]["next_heartbeat_at"] is None  # nosec B101
    assert migrated_rows["task_cancelled"]["next_heartbeat_at"] is None  # nosec B101
    assert migrated_rows["task_queued"]["next_heartbeat_at"] == stale_at  # nosec B101
    assert {row["updated_at"] for row in migrated_rows.values()} == {created_at}  # nosec B101


def test_task_and_schedule_forms_accept_empty_optional_references(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)

    task_response = client.post(
        "/tasks",
        json={
            "title": "Unassigned task",
            "assigned_agent_id": "",
            "credential_id": "",
            "heartbeat_seconds": None,
        },
    )
    assert task_response.status_code == 201  # nosec B101
    assert task_response.json()["assigned_agent_id"] is None  # nosec B101
    assert task_response.json()["credential_id"] is None  # nosec B101

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
    assert schedule_response.status_code == 201  # nosec B101
    assert schedule_response.json()["assigned_agent_id"] is None  # nosec B101
    assert schedule_response.json()["credential_id"] is None  # nosec B101
    require_equal(schedule_response.json()["catch_up_policy"], "run_once", "schedules should default to run_once")


def test_schedule_creation_rejects_invalid_catch_up_policy(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)

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
    setup_demo(client)

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
    setup_demo(client)

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

def test_legacy_schedule_priority_is_normalized_without_blocking_due_runs(tmp_path: Path) -> None:
    store = HivemindStore(tmp_path / "legacy-schedule-priority.db")
    client = TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")
    setup_demo(client)
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

    assert run_response.status_code == 200  # nosec B101
    created_tasks = {task["title"]: task for task in run_response.json()["created_tasks"]}
    assert set(created_tasks) == {"Legacy review task", "Healthy review task"}  # nosec B101
    assert created_tasks["Legacy review task"]["priority"] == "normal"  # nosec B101
    assert created_tasks["Healthy review task"]["priority"] == "low"  # nosec B101

    schedules = {schedule["id"]: schedule for schedule in client.get("/schedules").json()}
    assert schedules[legacy_schedule["id"]]["priority"] == "normal"  # nosec B101
    assert schedules[healthy_schedule["id"]]["priority"] == "low"  # nosec B101

    normalized_event = next(
        event
        for event in client.get("/audit-events").json()
        if event["type"] == "schedule.priority.normalized" and event["target_id"] == legacy_schedule["id"]
    )
    assert normalized_event["actor_id"] == me["id"]  # nosec B101
    assert normalized_event["decision"] == "allowed"  # nosec B101
    assert normalized_event["reason"] == "legacy schedule priority normalized"  # nosec B101
    assert normalized_event["metadata"] == {  # nosec B101
        "from_priority": "legacy-urgent",
        "to_priority": "normal",
    }


def test_schedule_creation_normalizes_offset_next_run_at(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
    offset_zone = timezone(timedelta(hours=14))
    future_at = (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=5)).astimezone(offset_zone)

    response = client.post(
        "/schedules",
        json={
            "name": "Offset input schedule",
            "interval_seconds": 60,
            "task_title": "Offset input scheduled task",
            "next_run_at": future_at.isoformat(),
        },
    )

    require_equal(response.status_code, 201, "schedule creation should accept offset-aware next_run_at")
    stored_next_run_at = datetime.fromisoformat(response.json()["next_run_at"])
    require_equal(stored_next_run_at.tzinfo, timezone.utc, "schedule creation should store next_run_at in UTC")
    require_equal(stored_next_run_at, future_at.astimezone(timezone.utc), "schedule creation should preserve the instant")


def test_bad_task_schedule_and_heartbeat_references_return_4xx(tmp_path: Path) -> None:
    client = client_for(tmp_path)
    setup_demo(client)
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
    heartbeat_note = f"token={secrets.token_hex(8)}"

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

    allowed_task = client.post(
        "/tasks",
        json={
            "title": "Allowed credential binding",
            "assigned_agent_id": primary_agent["id"],
            "credential_id": scoped_credential["id"],
        },
    ).json()
    bad_task_update_binding = client.patch(
        f"/tasks/{allowed_task['id']}",
        json={"assigned_agent_id": secondary_agent["id"]},
    )
    require_equal(bad_task_update_binding.status_code, 400, "task updates should preserve credential agent scope")
    require_equal(
        bad_task_update_binding.json()["detail"],
        f"assigned_agent_id is not allowed to use credential {scoped_credential['id']}: {secondary_agent['id']}",
        "task update binding rejection should explain the disallowed agent",
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
        json={"agent_id": "agent_missing", "note": heartbeat_note},
    )
    require_equal(bad_heartbeat_agent.status_code, 400, "unknown heartbeat agent should be rejected")
    require_equal(
        bad_heartbeat_agent.json()["detail"],
        "agent_id references unknown agent: agent_missing",
        "heartbeat rejection should explain the missing agent reference",
    )
    audit_events = client.get("/audit-events").json()
    require_true(
        any(
            event["type"] == "task.heartbeat.denied"
            and event["actor_id"].startswith("user_")
            and event["target_id"] == task["id"]
            and event["reason"] == "agent_id references unknown agent: agent_missing"
            and event["metadata"] == {"note_present": True, "note_length": len(heartbeat_note)}
            for event in audit_events
        ),
        "denied heartbeat should be audited without the raw note",
    )
    require_true(heartbeat_note not in str(audit_events), "raw heartbeat note should not appear in denied audit events")

    mismatched_heartbeat_agent = client.post(
        f"/tasks/{task['id']}/heartbeats",
        json={"agent_id": secondary_agent["id"], "note": heartbeat_note},
    )
    require_equal(mismatched_heartbeat_agent.status_code, 400, "mismatched heartbeat agent should be rejected")
    require_equal(
        mismatched_heartbeat_agent.json()["detail"],
        f"agent_id does not match assigned agent for task {task['id']}: {secondary_agent['id']}",
        "heartbeat rejection should explain the assigned-agent mismatch",
    )
    audit_events = client.get("/audit-events").json()
    require_true(
        any(
            event["type"] == "task.heartbeat.denied"
            and event["target_id"] == task["id"]
            and event["reason"] == f"agent_id does not match assigned agent for task {task['id']}: {secondary_agent['id']}"
            and event["metadata"] == {"note_present": True, "note_length": len(heartbeat_note)}
            for event in audit_events
        ),
        "mismatched heartbeat should be audited without the raw note",
    )
    require_true(heartbeat_note not in str(audit_events), "raw heartbeat note should not appear in denied audit events")


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

    client = TestClient(create_app(HivemindStore(db_path), start_scheduler=False), base_url="https://testserver")
    response = client.post("/auth/login", json={"username": "admin", "password": TEST_PASSWORD})

    assert response.status_code == 200  # nosec B101
    assert response.json()["user"]["username"] == "admin"  # nosec B101


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
