"""Auth endpoints: password-only login, logout, status."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.auth import (
    clear_session_cookie,
    issue_session_value,
    request_is_authenticated,
    set_session_cookie,
    verify_password,
)
from app.core.config import get_settings

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=512)


class AuthStatusResponse(BaseModel):
    authenticated: bool
    auth_enabled: bool = False


class LoginResponse(BaseModel):
    ok: bool = True
    authenticated: bool = True
    auth_enabled: bool = True


def _auth_enabled() -> bool:
    return bool(get_settings().auth_enabled)


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, response: Response) -> LoginResponse | JSONResponse:
    """Issue a session cookie when the shared password matches.

    When AUTH_ENABLED=false the app is public and password login is disabled.
    """
    if not _auth_enabled():
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Авторизация отключена",
                "message": "Авторизация отключена (публичный режим)",
                "code": "auth_disabled",
            },
        )

    if not verify_password(body.password):
        return JSONResponse(
            status_code=401,
            content={
                "detail": "Неверный пароль",
                "message": "Неверный пароль",
                "code": "invalid_credentials",
            },
        )
    set_session_cookie(response, issue_session_value())
    return LoginResponse(ok=True, authenticated=True, auth_enabled=True)


@router.post("/logout", response_model=AuthStatusResponse)
async def logout(response: Response) -> AuthStatusResponse:
    if not _auth_enabled():
        # Public mode: nothing to clear; stay "authenticated".
        return AuthStatusResponse(authenticated=True, auth_enabled=False)
    clear_session_cookie(response)
    return AuthStatusResponse(authenticated=False, auth_enabled=True)


@router.get("/status", response_model=AuthStatusResponse)
async def status(request: Request) -> AuthStatusResponse:
    enabled = _auth_enabled()
    return AuthStatusResponse(
        authenticated=request_is_authenticated(request),
        auth_enabled=enabled,
    )
