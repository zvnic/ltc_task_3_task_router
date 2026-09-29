"""Password-only session auth via signed HttpOnly cookie."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Final

from fastapi import Request, Response

from app.core.config import get_settings

COOKIE_NAME: Final = "beeline_session"
SESSION_TTL_SECONDS: Final = 12 * 60 * 60
_PUBLIC_EXACT: Final = frozenset(
    {
        "/api/v1/auth/login",
        "/api/v1/auth/status",
        "/api/v1/auth/logout",
        "/api/v1/health",
        "/api/v1/health/ready",
    }
)


def _signing_secret() -> bytes:
    settings = get_settings()
    raw = (settings.auth_secret or "").strip() or (settings.app_password or "").strip()
    if not raw:
        raw = "dev-insecure-auth-secret"
    return hashlib.sha256(raw.encode("utf-8")).digest()


def verify_password(password: str) -> bool:
    expected = (get_settings().app_password or "").strip()
    if not expected:
        return False
    return hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8"))


def issue_session_value() -> str:
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    payload = f"v1|{ts}|{nonce}"
    sig = hmac.new(_signing_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def validate_session_value(value: str | None) -> bool:
    if not value:
        return False
    parts = value.split("|")
    if len(parts) != 4 or parts[0] != "v1":
        return False
    payload = f"{parts[0]}|{parts[1]}|{parts[2]}"
    expected = hmac.new(_signing_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, parts[3]):
        return False
    try:
        ts = int(parts[1])
    except ValueError:
        return False
    return (time.time() - ts) <= SESSION_TTL_SECONDS


def set_session_cookie(response: Response, value: str) -> None:
    # Local compose serves plain HTTP on 127.0.0.1 — Secure cookies would never stick.
    response.set_cookie(
        key=COOKIE_NAME,
        value=value,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


def request_is_authenticated(request: Request) -> bool:
    """True when auth is disabled (public mode) or the session cookie is valid."""
    if not get_settings().auth_enabled:
        return True
    return validate_session_value(request.cookies.get(COOKIE_NAME))


def is_public_path(path: str, method: str = "GET") -> bool:
    """Paths that must stay reachable without a session cookie."""
    normalized = path.rstrip("/") or "/"
    # Always allow CORS preflight
    if method.upper() == "OPTIONS":
        return True
    if normalized in _PUBLIC_EXACT or path in _PUBLIC_EXACT:
        return True
    # /api/v1/health and nested readiness already listed; keep prefix safety
    if normalized.startswith("/api/v1/health"):
        return True
    return False
