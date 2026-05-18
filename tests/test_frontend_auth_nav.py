from pathlib import Path

from fastapi.testclient import TestClient

from hivemind.api import create_app
from hivemind.store import HivemindStore


def client_for(tmp_path: Path) -> TestClient:
    store = HivemindStore(tmp_path / "hivemind.db")
    return TestClient(create_app(store, start_scheduler=False), base_url="https://testserver")


def require_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_workspace_nav_is_hidden_until_session_is_loaded(tmp_path: Path) -> None:
    response = client_for(tmp_path).get("/")

    require_true(
        '<nav id="workspace-nav" class="page-nav" aria-label="Workspace" hidden>' in response.text,
        "frontend should hide workspace navigation until the operator is signed in",
    )
    require_true(
        '<button id="refresh-button" type="button" hidden>sync</button>' in response.text,
        "frontend should hide refresh until the operator is signed in",
    )
    require_true(
        '<button id="logout-button" type="button" hidden>logout</button>' in response.text,
        "frontend should hide logout until the operator is signed in",
    )
