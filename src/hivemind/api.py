from __future__ import annotations

from importlib.resources import files
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from hivemind.agents import AgentError
from hivemind.credentials import CredentialError
from hivemind.runtime import HivemindRuntime


class SpawnAgentRequest(BaseModel):
    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    provider: str = Field(default="local", min_length=1)
    model: str = Field(default="deterministic-policy", min_length=1)


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


def create_app() -> FastAPI:
    runtime = HivemindRuntime()
    static_dir = files("hivemind").joinpath("static")
    app = FastAPI(
        title="Hivemind",
        version="0.1.0",
        description="Security-focused swarm agent runtime with JIT credential leases.",
    )
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    def frontend() -> FileResponse:
        return FileResponse(static_dir.joinpath("index.html"))

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "hivemind"}

    @app.get("/config")
    def read_config() -> dict[str, Any]:
        return runtime.config.public_view()

    @app.get("/agents")
    def list_agents() -> list[dict[str, Any]]:
        return [agent.__dict__ for agent in runtime.agents.list()]

    @app.post("/agents", status_code=201)
    def spawn_agent(request: SpawnAgentRequest) -> dict[str, Any]:
        agent = runtime.agents.spawn(
            name=request.name,
            role=request.role,
            provider=request.provider,
            model=request.model,
        )
        return agent.__dict__

    @app.get("/credentials")
    def list_credentials() -> list[dict[str, Any]]:
        return [credential.public_view() for credential in runtime.vault.list()]

    @app.post("/credential-leases", status_code=201)
    def create_credential_lease(request: CreateLeaseRequest) -> dict[str, Any]:
        try:
            runtime.agents.get(request.agent_id)
            lease = runtime.credentials.request_lease(
                credential_id=request.credential_id,
                agent_id=request.agent_id,
                action=request.action,
                intent=request.intent,
                ttl_seconds=request.ttl_seconds,
            )
            body = lease.public_view()
            body["lease_token"] = lease.token
            return body
        except (AgentError, CredentialError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.get("/credential-leases")
    def list_credential_leases() -> list[dict[str, Any]]:
        return [lease.public_view() for lease in runtime.credentials.list_leases()]

    @app.post("/credential-actions")
    def perform_credential_action(request: PerformCredentialActionRequest) -> dict[str, Any]:
        try:
            return runtime.credentials.perform_action(
                lease_token=request.lease_token,
                action=request.action,
                payload=request.payload,
            )
        except CredentialError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.get("/audit-events")
    def list_audit_events() -> list[dict[str, Any]]:
        return [event.public_view() for event in runtime.credentials.audit_events()]

    return app
