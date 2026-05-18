from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging
import os
from importlib.resources import files
from threading import Event, Lock, Thread
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

import httpx
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from hivemind.oauth import (
    OAuthConfigurationError,
    SecretBox,
    build_pkce_pair,
    load_oauth_providers_from_env,
)
from hivemind.models import TaskPriority, TaskStatus
from hivemind.store import HivemindStore, SessionUser, StoreError, StoreNotFoundError, StoreValidationError


SESSION_COOKIE = "hivemind_session"
OAUTH_FAILED_EVENT = "credential.oauth.failed"
LOGGER = logging.getLogger(__name__)
SCHEDULER_INTERVAL_SECONDS = 5
SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS = 5
SCHEDULER_LOCK_WAIT_SECONDS = 0.1


@dataclass(frozen=True)
class _SchedulerHandle:
    thread: Thread
    stop: Event


def _scheduler_loop(db: HivemindStore, scheduler_stop: Event, scheduler_run_lock: Lock) -> None:
    while not scheduler_stop.is_set():
        if not scheduler_run_lock.acquire(timeout=SCHEDULER_LOCK_WAIT_SECONDS):
            continue
        try:
            if scheduler_stop.is_set():
                return
            try:
                db.run_due_schedules_once()
            except Exception as exc:
                LOGGER.warning("background scheduler pass failed: %s", exc.__class__.__name__)
        finally:
            scheduler_run_lock.release()
        scheduler_stop.wait(SCHEDULER_INTERVAL_SECONDS)


def _should_start_background_scheduler(start_scheduler: bool | None) -> bool:
    should_start = start_scheduler
    if should_start is None:
        should_start = os.getenv("HIVEMIND_SCHEDULER", "true").lower() == "true"
    return should_start


def _start_background_scheduler(db: HivemindStore, scheduler_run_lock: Lock) -> _SchedulerHandle:
    scheduler_stop = Event()
    thread = Thread(
        target=_scheduler_loop,
        args=(db, scheduler_stop, scheduler_run_lock),
        name="hivemind-scheduler",
        daemon=True,
    )
    thread.start()
    return _SchedulerHandle(thread=thread, stop=scheduler_stop)


def _reuse_or_start_background_scheduler(app: FastAPI, db: HivemindStore, scheduler_run_lock: Lock) -> _SchedulerHandle:
    existing_handle = getattr(app.state, "scheduler_handle", None)
    if isinstance(existing_handle, _SchedulerHandle):
        if existing_handle.thread.is_alive() and not existing_handle.stop.is_set():
            LOGGER.warning("background scheduler is already running; reusing scheduler thread")
            return existing_handle
        if existing_handle.thread.is_alive():
            LOGGER.warning("background scheduler is still stopping; starting replacement scheduler thread")

    handle = _start_background_scheduler(db, scheduler_run_lock)
    app.state.scheduler_handle = handle
    app.state.scheduler_thread = handle.thread
    return handle


def _stop_background_scheduler(app: FastAPI, handle: _SchedulerHandle | None) -> None:
    if handle is None:
        return

    handle.stop.set()
    handle.thread.join(timeout=SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS)
    if handle.thread.is_alive():
        LOGGER.error(
            "background scheduler did not stop within %s seconds",
            SCHEDULER_SHUTDOWN_TIMEOUT_SECONDS,
        )
        return

    if getattr(app.state, "scheduler_handle", None) is handle:
        app.state.scheduler_handle = None
        app.state.scheduler_thread = None


def _scheduler_lifespan(db: HivemindStore, start_scheduler: bool | None, scheduler_run_lock: Lock):
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        handle: _SchedulerHandle | None = None
        if _should_start_background_scheduler(start_scheduler):
            handle = _reuse_or_start_background_scheduler(app, db, scheduler_run_lock)
        try:
            yield
        finally:
            _stop_background_scheduler(app, handle)

    return lifespan


class SetupRequest(BaseModel):
    username: str = Field(min_length=3)
    password: str = Field(min_length=12)
    password_confirm: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class SpawnAgentRequest(BaseModel):
    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    provider: str = Field(default="local", min_length=1)
    model: str | None = Field(default=None, min_length=1)
    system_prompt: str = ""


class UpdateAgentStatusRequest(BaseModel):
    status: str = Field(pattern="^(idle|queued|running|blocked|done|failed|working)$")


class CreateCredentialRequest(BaseModel):
    name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    secret_ref: str | None = Field(default=None, min_length=6)
    secret_value: str | None = Field(default=None, min_length=1)
    allowed_agents: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)
    approval_required_actions: list[str] = Field(default_factory=list)
    max_ttl_seconds: int = Field(default=300, ge=1, le=3600)
    require_intent: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class StartOAuthCredentialRequest(BaseModel):
    provider: str = Field(min_length=1)
    name: str = Field(min_length=1)
    allowed_agents: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)
    approval_required_actions: list[str] = Field(default_factory=list)
    max_ttl_seconds: int = Field(default=300, ge=1, le=3600)
    require_intent: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateLeaseRequest(BaseModel):
    credential_id: str
    agent_id: str
    action: str
    intent: str
    ttl_seconds: int | None = Field(default=None, gt=0)


class PerformCredentialActionRequest(BaseModel):
    lease_token: str
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)


class CreateTaskRequest(BaseModel):
    title: str = Field(min_length=1)
    description: str = ""
    status: TaskStatus = TaskStatus.QUEUED
    priority: TaskPriority = TaskPriority.NORMAL
    assigned_agent_id: str | None = None
    credential_id: str | None = None
    action: str = ""
    intent: str = ""
    heartbeat_seconds: int | None = Field(default=None, ge=30)


class UpdateTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1)
    description: str | None = None
    priority: TaskPriority | None = None
    assigned_agent_id: str | None = None
    credential_id: str | None = None
    action: str | None = None
    intent: str | None = None
    heartbeat_seconds: int | None = Field(default=None, ge=30)


class UpdateTaskStatusRequest(BaseModel):
    status: TaskStatus


class HeartbeatRequest(BaseModel):
    agent_id: str | None = None
    note: str = Field(default="still working", min_length=1)


class RunTaskRequest(BaseModel):
    input: str = ""


class CreateScheduleRequest(BaseModel):
    name: str = Field(min_length=1)
    enabled: bool = True
    interval_seconds: int = Field(ge=60)
    catch_up_policy: Literal["skip_missed", "run_once", "backfill"] = "run_once"
    task_title: str = Field(min_length=1)
    task_description: str = ""
    priority: TaskPriority = TaskPriority.NORMAL
    assigned_agent_id: str | None = None
    credential_id: str | None = None
    action: str = ""
    intent: str = ""
    next_run_at: str | None = None


class UpdateScheduleRequest(BaseModel):
    enabled: bool


def create_app(store: HivemindStore | None = None, *, start_scheduler: bool | None = None) -> FastAPI:
    db = store or HivemindStore.from_env()
    config = db.config
    oauth_providers = load_oauth_providers_from_env()
    secret_box = SecretBox.from_env()
    static_dir = files("hivemind").joinpath("static")
    scheduler_run_lock = Lock()

    app = FastAPI(
        title="Hivemind",
        version="0.2.0",
        description="Security-focused swarm agent runtime with JIT credential leases.",
        lifespan=_scheduler_lifespan(db, start_scheduler, scheduler_run_lock),
    )
    app.state.store = db
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def serve_frontend() -> FileResponse:
        return FileResponse(static_dir.joinpath("index.html"))

    def require_user(session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None) -> SessionUser:
        user = db.get_session_user(session)
        if user is None:
            raise HTTPException(status_code=401, detail="authentication required")
        return user

    def session_cookie_secure() -> bool:
        return not config.development_mode

    def set_session_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            secure=session_cookie_secure(),
            samesite="lax",
            max_age=12 * 60 * 60,
            path="/",
        )

    def oauth_frontend_redirect(status: str, detail: str) -> RedirectResponse:
        query = urlencode({"oauth": status, "detail": detail})
        return RedirectResponse(url=f"/?{query}", status_code=303)

    @app.get("/", include_in_schema=False)
    def frontend() -> FileResponse:
        return serve_frontend()

    @app.get("/control", include_in_schema=False)
    @app.get("/control/{path:path}", include_in_schema=False)
    def frontend_control(path: str = "") -> FileResponse:
        return serve_frontend()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "hivemind"}

    @app.get("/setup-state")
    def setup_state() -> dict[str, bool]:
        return {"setup_complete": db.is_setup_complete()}

    @app.post("/auth/setup", status_code=201)
    def setup(request: SetupRequest, response: Response) -> dict[str, Any]:
        if request.password_confirm is not None and request.password_confirm != request.password:
            raise HTTPException(status_code=400, detail="password confirmation does not match")
        try:
            user = db.setup_admin(request.username, request.password)
            token, user = db.login(request.username, request.password)
            set_session_cookie(response, token)
            return {"user": user}
        except StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/auth/login")
    def login(request: LoginRequest, response: Response) -> dict[str, Any]:
        try:
            token, user = db.login(request.username, request.password)
            set_session_cookie(response, token)
            return {"user": user}
        except StoreError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @app.post("/auth/logout")
    def logout(response: Response, session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None) -> dict[str, bool]:
        db.logout(session)
        response.delete_cookie(
            SESSION_COOKIE,
            path="/",
            secure=session_cookie_secure(),
            httponly=True,
            samesite="lax",
        )
        return {"ok": True}

    @app.get("/me")
    def me(user: SessionUser = Depends(require_user)) -> dict[str, Any]:
        return {"id": user.id, "username": user.username, "role": user.role}

    @app.get("/config")
    def read_config(user: SessionUser = Depends(require_user)) -> dict[str, Any]:
        return config.public_view()

    @app.get("/agents")
    def list_agents(user: SessionUser = Depends(require_user)) -> list[dict[str, Any]]:
        return db.list_agents()

    @app.post("/agents", status_code=201)
    def spawn_agent(request: SpawnAgentRequest, user: SessionUser = Depends(require_user)) -> dict[str, Any]:
        return db.create_agent(request.model_dump(), actor_id=user.id)

    @app.patch("/agents/{agent_id}/status")
    def update_agent_status(
        agent_id: str,
        request: UpdateAgentStatusRequest,
        user: SessionUser = Depends(require_user),
    ) -> dict[str, Any]:
        try:
            return db.update_agent_status(agent_id, request.status, actor_id=user.id)
        except StoreNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/credentials")
    def list_credentials(user: SessionUser = Depends(require_user)) -> list[dict[str, Any]]:
        return db.list_credentials()

    @app.get("/oauth/providers")
    def list_oauth_providers(user: SessionUser = Depends(require_user)) -> list[dict[str, Any]]:
        return [provider.public_view(has_secret_store=secret_box is not None) for provider in oauth_providers.values()]

    @app.post("/oauth/credentials/start", status_code=201)
    def start_oauth_credential(
        request: StartOAuthCredentialRequest,
        http_request: Request,
        user: SessionUser = Depends(require_user),
    ) -> dict[str, str]:
        provider = oauth_providers.get(request.provider)
        if provider is None:
            raise HTTPException(status_code=404, detail=f"unknown oauth provider: {request.provider}")
        if secret_box is None:
            raise HTTPException(status_code=400, detail="Set HIVEMIND_SECRETS_KEY to enable broker-side OAuth token storage.")
        available, reason = provider.availability(has_secret_store=True)
        if not available:
            raise HTTPException(status_code=400, detail=reason or f"oauth provider unavailable: {provider.id}")
        if not any(action.strip() for action in request.allowed_actions):
            raise HTTPException(status_code=400, detail="credential must allow at least one action")
        verifier, challenge = build_pkce_pair()
        state = db.create_oauth_state(
            user_id=user.id,
            provider=provider.id,
            pkce_verifier=verifier,
            credential_payload=request.model_dump(exclude={"provider"}),
        )
        redirect_uri = str(http_request.url_for("oauth_callback", provider=provider.id))
        try:
            authorize_url = provider.build_authorize_url(
                redirect_uri=redirect_uri,
                state=state,
                code_challenge=challenge,
            )
        except OAuthConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"authorize_url": authorize_url}

    @app.post("/credentials", status_code=201)
    def create_credential(request: CreateCredentialRequest, user: SessionUser = Depends(require_user)) -> dict[str, Any]:
        payload = request.model_dump()
        secret_ref = str(payload.get("secret_ref") or "").strip()
        secret_value = payload.pop("secret_value", None)
        has_secret_ref = bool(secret_ref)
        has_secret_value = secret_value is not None
        if has_secret_ref == has_secret_value:
            raise HTTPException(status_code=400, detail="provide exactly one of secret_ref or secret_value")
        payload["secret_ref"] = secret_ref or None
        try:
            if has_secret_value:
                if secret_box is None:
                    raise HTTPException(
                        status_code=400,
                        detail="Set HIVEMIND_SECRETS_KEY to enable broker-side local secret storage.",
                    )
                return db.create_managed_credential(
                    payload,
                    secret_value=secret_value,
                    secret_box=secret_box,
                )
            return db.create_credential(payload)
        except StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/oauth/callback/{provider}", name="oauth_callback")
    def oauth_callback(
        provider: str,
        http_request: Request,
        state: str | None = None,
        code: str | None = None,
        error: str | None = None,
        error_description: str | None = None,
        session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    ) -> RedirectResponse:
        user = db.get_session_user(session)
        if user is None:
            return oauth_frontend_redirect("error", "Sign in again to finish OAuth.")
        provider_config = oauth_providers.get(provider)
        if provider_config is None:
            return oauth_frontend_redirect("error", f"Unknown OAuth provider: {provider}.")
        if secret_box is None:
            return oauth_frontend_redirect("error", "Broker-side OAuth token storage is not configured.")
        if not state:
            return oauth_frontend_redirect("error", "Missing OAuth state.")
        try:
            oauth_state = db.consume_oauth_state(state_id=state, provider=provider, user_id=user.id)
        except StoreError as exc:
            db.audit(
                OAUTH_FAILED_EVENT,
                user.id,
                provider,
                "denied",
                str(exc),
                {"provider": provider},
            )
            return oauth_frontend_redirect("error", str(exc))
        if error:
            db.audit(
                OAUTH_FAILED_EVENT,
                user.id,
                provider,
                "denied",
                error_description or error,
                {"provider": provider},
            )
            return oauth_frontend_redirect("error", error_description or error)
        if not code:
            db.audit(
                OAUTH_FAILED_EVENT,
                user.id,
                provider,
                "denied",
                "Missing OAuth authorization code.",
                {"provider": provider},
            )
            return oauth_frontend_redirect("error", "Missing OAuth authorization code.")
        redirect_uri = str(http_request.url_for("oauth_callback", provider=provider))
        try:
            response = httpx.post(
                provider_config.token_url,
                data=provider_config.build_token_payload(
                    code=code,
                    redirect_uri=redirect_uri,
                    code_verifier=oauth_state["pkce_verifier"],
                ),
                headers={"Accept": "application/json"},
                timeout=20.0,
            )
            response.raise_for_status()
            token_payload = response.json()
            credential = db.create_oauth_credential(
                provider=provider_config.credential_provider,
                token_payload=token_payload,
                requested_credential=oauth_state["credential_payload"],
                secret_box=secret_box,
                actor_id=user.id,
            )
        except (OAuthConfigurationError, StoreError) as exc:
            db.audit(
                OAUTH_FAILED_EVENT,
                user.id,
                provider,
                "denied",
                str(exc),
                {"provider": provider},
            )
            return oauth_frontend_redirect("error", str(exc))
        except (ValueError, httpx.HTTPError) as exc:
            db.audit(
                OAUTH_FAILED_EVENT,
                user.id,
                provider,
                "denied",
                str(exc),
                {"provider": provider},
            )
            return oauth_frontend_redirect("error", str(exc))
        return oauth_frontend_redirect("connected", f"{credential['name']} connected.")

    @app.post("/credential-leases", status_code=201)
    def create_credential_lease(request: CreateLeaseRequest, user: SessionUser = Depends(require_user)) -> dict[str, Any]:
        try:
            _, lease = db.request_lease(
                credential_id=request.credential_id,
                agent_id=request.agent_id,
                action=request.action,
                intent=request.intent,
                ttl_seconds=request.ttl_seconds,
            )
            return lease
        except StoreError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.get("/credential-leases")
    def list_credential_leases(user: SessionUser = Depends(require_user)) -> list[dict[str, Any]]:
        return db.list_leases()

    @app.post("/credential-leases/{lease_id}/approve")
    def approve_credential_lease(lease_id: str, user: SessionUser = Depends(require_user)) -> dict[str, Any]:
        try:
            _, lease = db.approve_lease(lease_id, user.id)
            return lease
        except StoreNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except StoreError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/credential-leases/{lease_id}/deny")
    def deny_credential_lease(lease_id: str, user: SessionUser = Depends(require_user)) -> dict[str, Any]:
        try:
            return db.deny_lease(lease_id, user.id)
        except StoreNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except StoreError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/credential-actions")
    def perform_credential_action(request: PerformCredentialActionRequest, user: SessionUser = Depends(require_user)) -> dict[str, Any]:
        try:
            return db.perform_credential_action(
                lease_token=request.lease_token,
                action=request.action,
                payload=request.payload,
            )
        except StoreError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.get("/tasks")
    def list_tasks(user: SessionUser = Depends(require_user)) -> list[dict[str, Any]]:
        return db.list_tasks()

    @app.post("/tasks", status_code=201)
    def create_task(request: CreateTaskRequest, user: SessionUser = Depends(require_user)) -> dict[str, Any]:
        try:
            return db.create_task(request.model_dump(), actor_id=user.id)
        except StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/tasks/{task_id}")
    def update_task(task_id: str, request: UpdateTaskRequest, user: SessionUser = Depends(require_user)) -> dict[str, Any]:
        try:
            return db.update_task(task_id, request.model_dump(exclude_unset=True), actor_id=user.id)
        except StoreValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except StoreNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.patch("/tasks/{task_id}/status")
    def update_task_status(task_id: str, request: UpdateTaskStatusRequest, user: SessionUser = Depends(require_user)) -> dict[str, Any]:
        try:
            return db.update_task_status(task_id, request.status, actor_id=user.id)
        except StoreValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except StoreNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/tasks/{task_id}/run", status_code=201)
    def run_task(task_id: str, request: RunTaskRequest, user: SessionUser = Depends(require_user)) -> dict[str, Any]:
        try:
            return db.run_task(task_id, request.input)
        except StoreNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except StoreValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except StoreError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/tasks/{task_id}/heartbeats", status_code=201)
    def record_heartbeat(task_id: str, request: HeartbeatRequest, user: SessionUser = Depends(require_user)) -> dict[str, Any]:
        try:
            return db.record_heartbeat(task_id, request.agent_id, request.note, actor_id=user.id)
        except StoreValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except StoreNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/heartbeats")
    def list_heartbeats(user: SessionUser = Depends(require_user), task_id: str | None = None) -> list[dict[str, Any]]:
        return db.list_heartbeats(task_id)

    @app.get("/schedules")
    def list_schedules(user: SessionUser = Depends(require_user)) -> list[dict[str, Any]]:
        return db.list_schedules()

    @app.post("/schedules", status_code=201)
    def create_schedule(request: CreateScheduleRequest, user: SessionUser = Depends(require_user)) -> dict[str, Any]:
        try:
            return db.create_schedule(request.model_dump(), actor_id=user.id)
        except StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/schedules/{schedule_id}")
    def update_schedule(schedule_id: str, request: UpdateScheduleRequest, user: SessionUser = Depends(require_user)) -> dict[str, Any]:
        try:
            return db.update_schedule_enabled(schedule_id, request.enabled)
        except StoreNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/schedules/run-due")
    def run_due_schedules(user: SessionUser = Depends(require_user)) -> dict[str, Any]:
        try:
            return {"created_tasks": db.run_due_schedules_once(actor_id=user.id)}
        except StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/audit-events")
    def list_audit_events(user: SessionUser = Depends(require_user)) -> list[dict[str, Any]]:
        return db.list_audit_events()

    return app
