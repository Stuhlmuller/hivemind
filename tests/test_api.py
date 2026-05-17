from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from hivemind.api import create_app
from hivemind.store import HivemindStore


def client_for(tmp_path: Path) -> TestClient:
    store = HivemindStore(tmp_path / "hivemind.db")
    return TestClient(create_app(store, start_scheduler=False))


def setup(client: TestClient) -> None:
    response = client.post(
        "/auth/setup",
        json={"email": "admin@hivemind.local", "password": "hivemind-password"},
    )
    assert response.status_code == 201


def test_frontend_is_served(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert "Hivemind" in response.text
    assert "/static/app.js" in response.text


def test_config_requires_login_and_exposes_reviewer_after_setup(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    assert client.get("/config").status_code == 401
    setup(client)
    response = client.get("/config")

    assert response.status_code == 200
    assert response.json()["intent_reviewer"]["provider"] == "local"


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

