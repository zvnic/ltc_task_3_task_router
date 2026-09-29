"""Password-only session auth (enabled and public modes)."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

# Ensure auth settings before app import / settings cache.
# Default these tests to auth-enabled so the password gate is exercised.
os.environ["AUTH_ENABLED"] = "true"
os.environ.setdefault("APP_PASSWORD", "test-password-auth-xyz")
os.environ.setdefault("AUTH_SECRET", "test-auth-secret-xyz-0123456789")
os.environ.setdefault("APP_ENV", "test")

from app.core.auth import COOKIE_NAME
from app.core.config import get_settings
from app.db import get_session
from app.main import app, required_schema_revisions

get_settings.cache_clear()


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("APP_PASSWORD", "test-password-auth-xyz")
    monkeypatch.setenv("AUTH_SECRET", "test-auth-secret-xyz-0123456789")
    monkeypatch.setenv("APP_ENV", "test")
    get_settings.cache_clear()

    from app.main import app as _app

    with TestClient(_app) as test_client:
        yield test_client

    get_settings.cache_clear()


def test_wrong_password_returns_401(client: TestClient) -> None:
    response = client.post("/api/v1/auth/login", json={"password": "not-the-password"})
    assert response.status_code == 401
    body = response.json()
    assert body.get("code") == "invalid_credentials"


def test_correct_password_sets_cookie_and_status(client: TestClient) -> None:
    login = client.post("/api/v1/auth/login", json={"password": "test-password-auth-xyz"})
    assert login.status_code == 200
    body = login.json()
    assert body.get("authenticated") is True
    assert body.get("auth_enabled") is True
    assert COOKIE_NAME in login.cookies

    status = client.get("/api/v1/auth/status")
    assert status.status_code == 200
    assert status.json() == {"authenticated": True, "auth_enabled": True}


def test_protected_endpoint_without_cookie_401(client: TestClient) -> None:
    # Fresh client has no session cookie
    client.cookies.clear()
    response = client.get("/api/v1/algorithms")
    assert response.status_code == 401
    body = response.json()
    assert body.get("code") == "unauthorized"


def test_protected_endpoint_with_cookie_200(client: TestClient) -> None:
    login = client.post("/api/v1/auth/login", json={"password": "test-password-auth-xyz"})
    assert login.status_code == 200

    response = client.get("/api/v1/algorithms")
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list) or (isinstance(payload, dict) and "items" in payload)


def test_health_ready_public(client: TestClient) -> None:
    client.cookies.clear()
    response = client.get("/api/v1/health/ready")
    # 200 when DB/schema ok, 503 when not — never 401
    assert response.status_code in (200, 503)
    if response.status_code == 200:
        assert response.json().get("status") == "ok" or "status" in response.json()


def test_health_uses_current_migration_head() -> None:
    assert required_schema_revisions() == frozenset({"0004_expected_duration"})


def test_health_rejects_outdated_schema(client: TestClient) -> None:
    class OutdatedRevisionResult:
        def scalars(self) -> OutdatedRevisionResult:
            return self

        def all(self) -> list[str]:
            return ["0001_initial"]

    class OutdatedSchemaSession:
        async def execute(self, _statement: object) -> OutdatedRevisionResult:
            return OutdatedRevisionResult()

        async def scalar(self, _statement: object) -> str:
            return "alembic_version"

    async def outdated_session():  # type: ignore[no-untyped-def]
        yield OutdatedSchemaSession()

    app.dependency_overrides[get_session] = outdated_session
    try:
        response = client.get("/api/v1/health/ready")
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert response.status_code == 503
    assert response.json()["code"] == "schema_outdated"
    assert response.json()["details"] == {
        "current_revisions": ["0001_initial"],
        "required_revisions": ["0004_expected_duration"],
    }


def test_logout_clears_session(client: TestClient) -> None:
    login = client.post("/api/v1/auth/login", json={"password": "test-password-auth-xyz"})
    assert login.status_code == 200
    logout = client.post("/api/v1/auth/logout")
    assert logout.status_code == 200
    assert logout.json() == {"authenticated": False, "auth_enabled": True}

    client.cookies.clear()
    status = client.get("/api/v1/auth/status")
    assert status.json() == {"authenticated": False, "auth_enabled": True}


# ---- Public mode (AUTH_ENABLED=false) ----


@pytest.fixture()
def public_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()
    os.environ["AUTH_ENABLED"] = "true"
    get_settings.cache_clear()


def test_public_mode_status_authenticated(public_client: TestClient) -> None:
    public_client.cookies.clear()
    status = public_client.get("/api/v1/auth/status")
    assert status.status_code == 200
    assert status.json() == {"authenticated": True, "auth_enabled": False}


def test_public_mode_protected_routes_open(public_client: TestClient) -> None:
    public_client.cookies.clear()
    response = public_client.get("/api/v1/algorithms")
    assert response.status_code != 401


def test_public_mode_login_disabled(public_client: TestClient) -> None:
    response = public_client.post("/api/v1/auth/login", json={"password": "anything"})
    assert response.status_code == 503
    assert response.json().get("code") == "auth_disabled"


def test_public_mode_logout_stays_authenticated(public_client: TestClient) -> None:
    logout = public_client.post("/api/v1/auth/logout")
    assert logout.status_code == 200
    assert logout.json() == {"authenticated": True, "auth_enabled": False}
