from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from hivemind.api import create_app
from hivemind.store import HivemindStore


def client_for(tmp_path: Path) -> TestClient:
    store = HivemindStore(tmp_path / "hivemind.db")
    return TestClient(create_app(store, start_scheduler=False))


def setup_admin(client: TestClient) -> None:
    response = client.post(
        "/auth/setup",
        json={"username": "operator", "password": "aaaaaaaaaaaa"},
    )
    assert response.status_code == 201


def test_frontend_does_not_ship_default_auth_credentials(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert 'name="username" value=' not in response.text
    assert 'name="password" type="password" value=' not in response.text
    assert "hivemind-password" not in response.text
    assert 'placeholder="operator"' in response.text
    assert 'placeholder="choose a long local password"' in response.text


def test_config_redacts_intent_reviewer_credential_ref(tmp_path: Path, monkeypatch) -> None:
    raw_ref = "env://HIVEMIND_INTENT_REVIEWER_TOKEN"
    monkeypatch.setenv("HIVEMIND_INTENT_REVIEWER_CREDENTIAL_REF", raw_ref)
    client = client_for(tmp_path)
    setup_admin(client)

    response = client.get("/config")

    assert response.status_code == 200
    reviewer = response.json()["intent_reviewer"]
    assert reviewer["provider"] == "local"
    assert reviewer["model"] == "deterministic-policy"
    assert reviewer["credential_ref"] == "env://HIV..."
    assert raw_ref not in response.text
