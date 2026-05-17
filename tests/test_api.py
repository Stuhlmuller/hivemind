from __future__ import annotations

from fastapi.testclient import TestClient

from hivemind.api import create_app


def test_frontend_is_served() -> None:
    client = TestClient(create_app())

    response = client.get("/")

    assert response.status_code == 200
    assert "Hivemind" in response.text
    assert "/static/app.js" in response.text


def test_config_endpoint_exposes_reviewer_without_secret_value() -> None:
    client = TestClient(create_app())

    response = client.get("/config")

    assert response.status_code == 200
    assert response.json()["intent_reviewer"]["provider"] == "local"
