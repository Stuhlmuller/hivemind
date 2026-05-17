from __future__ import annotations

import os
from importlib.resources import files
from threading import Event, Thread
from time import sleep
from typing import Annotated, Any

from fastapi import Cookie, Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from hivemind.config import HivemindConfig
from hivemind.store import HivemindStore, SessionUser, StoreError, StoreNotFoundError, StoreValidationError


SESSION_COOKIE = "hivemind_session"


class SetupRequest(BaseModel):
    username: str = Field(min_length=3)
    password: str = Field(min_length=12)


class LoginRequest(BaseModel):
    username: str
    password: str


class SpawnAgentRequest(BaseModel):
    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    provider: str = Field(default="local", min_length=1)
    model: str = Field(default="deterministic-policy", min_length=1)
    system_prompt: str = ""


class CreateCredentialRequest(BaseModel):
    name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    secret_ref: str = Field(min_length=6)
    allowed_agents: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)
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
    priority: str = "normal"
    assigned_agent_id: str | None = None
    credential_id: str | None = None
    action: str = ""
    intent: str = ""
    heartbeat_seconds: int | None = Field(default=None, ge=30)


class UpdateTaskStatusRequest(BaseModel):
    status: str = Field(pattern="^(queued|running|blocked|done|failed|cancelled)$")


class HeartbeatRequest(BaseModel):
    agent_id: str | None = None
    note: str = Field(default="still working", min_length=1)


class CreateScheduleRequest(BaseModel):
    name: str = Field(min_length=1)
    enabled: bool = True
    interval_seconds: int = Field(ge=60)
    task_title: str = Field(min_length=1)
    task_description: str = ""
    priority: str = "normal"
    assigned_agent_id: str | None = None
    credential_id: str | None = None
    action: str = ""
    intent: str = ""
    next_run_at: str | None = None


def create_app(store: HivemindStore | None = None, *, start_scheduler: bool | None = None) -> FastAPI:
    config = HivemindConfig.from_env()
    db = store or HivemindStore.from_env()
    static_dir = files("hivemind").joinpath("static")
    scheduler_stop = Event()

    app = FastAPI(
        title="Hivemind",
        version="0.2.0",
        description="Security-focused swarm agent runtime with JIT credential leases.",
    )
    app.state.store = db
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def require_user(session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None) -> SessionUser:
        user = db.get_session_user(session)
        if user is None:
            raise HTTPException(status_code=401, detail="authentication required")
        return user

    def set_session_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            secure=os.getenv("HIVEMIND_COOKIE_SECURE", "false").lower() == "true",
            samesite="lax",
            max_age=12 * 60 * 60,
            path="/",
        )

    def scheduler_loop() -> None:
        while not scheduler_stop.is_set():
            try:
                db.run_due_schedules_once()
            except Exception:
                pass
            sleep(5)

    @app.on_event("startup")
    def start_background_scheduler() -> None:
        should_start = start_scheduler
        if should_start is None:
            should_start = os.getenv("HIVEMIND_SCHEDULER", "true").lower() == "true"
        if should_start:
            thread = Thread(target=scheduler_loop, name="hivemind-scheduler", daemon=True)
            thread.start()
            app.state.scheduler_thread = thread

    @app.on_event("shutdown")
    def stop_background_scheduler() -> None:
        scheduler_stop.set()

    @app.get("/", include_in_schema=False)
    def frontend() -> FileResponse:
        return FileResponse(static_dir.joinpath("index.html"))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "hivemind"}

    @app.get("/setup-state")
    def setup_state() -> dict[str, bool]:
        return {"setup_complete": db.is_setup_complete()}

    @app.post("/auth/setup", status_code=201)
    def setup(request: SetupRequest, response: Response) -> dict[str, Any]:
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
        response.delete_cookie(SESSION_COOKIE, path="/")
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
        return db.create_agent(request.model_dump())

    @app.get("/credentials")
    def list_credentials(user: SessionUser = Depends(require_user)) -> list[dict[str, Any]]:
        return db.list_credentials()

    @app.post("/credentials", status_code=201)
    def create_credential(request: CreateCredentialRequest, user: SessionUser = Depends(require_user)) -> dict[str, Any]:
        try:
            return db.create_credential(request.model_dump())
        except StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
            return db.create_task(request.model_dump())
        except StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/tasks/{task_id}/status")
    def update_task_status(task_id: str, request: UpdateTaskStatusRequest, user: SessionUser = Depends(require_user)) -> dict[str, Any]:
        try:
            return db.update_task_status(task_id, request.status)
        except StoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/tasks/{task_id}/heartbeats", status_code=201)
    def record_heartbeat(task_id: str, request: HeartbeatRequest, user: SessionUser = Depends(require_user)) -> dict[str, Any]:
        try:
            return db.record_heartbeat(task_id, request.agent_id, request.note)
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
            return db.create_schedule(request.model_dump())
        except StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/schedules/run-due")
    def run_due_schedules(user: SessionUser = Depends(require_user)) -> dict[str, Any]:
        return {"created_tasks": db.run_due_schedules_once()}

    @app.get("/audit-events")
    def list_audit_events(user: SessionUser = Depends(require_user)) -> list[dict[str, Any]]:
        return db.list_audit_events()

    return app
