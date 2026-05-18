from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from hivemind.api import create_app
from hivemind.store import HivemindStore


def client_for(tmp_path: Path) -> TestClient:
    return TestClient(create_app(HivemindStore(tmp_path / "hivemind.db"), start_scheduler=False))


def require_equal(actual: object, expected: object, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def require_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def test_runtime_sidebar_frontend_routes_are_served(tmp_path: Path) -> None:
    client = client_for(tmp_path)

    routes = {
        "/control/agents": ('data-page-link="agents"', 'id="agents-page"', 'id="agents-list"'),
        "/control/agents/": ('data-page-link="agents"', 'id="agents-page"', 'id="agents-list"'),
        "/control/tasks": ('data-page-link="tasks"', 'id="tasks-page"', 'id="tasks-list"'),
        "/control/schedules": ('data-page-link="schedules"', 'id="schedules-page"', 'id="schedules-list"'),
        "/control/audit": ('data-page-link="audit"', 'id="audit-page"', 'id="audit-list"'),
    }

    for path, required_markup in routes.items():
        response = client.get(path)

        require_equal(response.status_code, 200, f"{path} should serve the frontend")
        for markup in required_markup:
            require_true(markup in response.text, f"{path} should include {markup}")


def test_frontend_route_selection_handles_trailing_control_paths() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    app_js = (repo_root / "src/hivemind/static/app.js").read_text(encoding="utf-8")

    require_true("function normalizePagePath(pathname)" in app_js, "frontend should normalize route paths")
    require_true(
        "pathname === routePath || pathname.startsWith(`${routePath}/`)" in app_js,
        "frontend should match trailing and nested control routes to their page",
    )
