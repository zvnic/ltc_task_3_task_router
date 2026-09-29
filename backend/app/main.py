import asyncio
import logging
from collections.abc import AsyncIterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.core.auth import is_public_path, request_is_authenticated
from app.core.config import get_settings
from app.core.errors import (
    ApiError,
    DomainError,
    domain_error_handler,
    validation_error_handler,
)
from app.db import dispose_engine, get_session
from app.routers import auth as auth_router
from app.routers.api import router

logger = logging.getLogger(__name__)


@lru_cache
def required_schema_revisions() -> frozenset[str]:
    """Return the migration heads shipped with this exact application image."""
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    return frozenset(ScriptDirectory.from_config(config).get_heads())


class AuthMiddleware(BaseHTTPMiddleware):
    """Require a valid session cookie for protected /api/v1/* routes when auth is enabled."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Public mode (AUTH_ENABLED=false): skip the password gate entirely.
        if not get_settings().auth_enabled:
            return await call_next(request)
        path = request.url.path
        if path.startswith("/api/v1") and not is_public_path(path, request.method):
            if not request_is_authenticated(request):
                return JSONResponse(
                    status_code=401,
                    content={
                        "code": "unauthorized",
                        "message": "Требуется авторизация.",
                        "details": {},
                        "request_id": getattr(request.state, "request_id", "unknown"),
                    },
                )
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.planning_pool = ProcessPoolExecutor(max_workers=1)
    app.state.planning_lock = asyncio.Lock()
    app.state.route_geometry_cache = {}
    yield
    pool = app.state.planning_pool
    pool.shutdown(wait=False, cancel_futures=True)
    await dispose_engine()


app = FastAPI(title="Task Router Диспетчерская", version="0.1.0", lifespan=lifespan)
app.add_exception_handler(DomainError, domain_error_handler)  # type: ignore[arg-type]
app.add_exception_handler(
    RequestValidationError,
    validation_error_handler,  # type: ignore[arg-type]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AuthMiddleware)


@app.middleware("http")
async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
    request.state.request_id = request.headers.get("x-request-id", str(uuid4()))
    try:
        response = await call_next(request)
    except DomainError:
        raise
    except Exception:
        logger.exception(
            "Unexpected request failure", extra={"request_id": request.state.request_id}
        )
        error = ApiError(
            code="internal_error",
            message="Внутренняя ошибка сервиса.",
            request_id=request.state.request_id,
        )
        return JSONResponse(status_code=500, content=error.model_dump(mode="json"))
    response.headers["x-request-id"] = request.state.request_id
    return response


@app.get("/api/v1/health/ready")
async def health_ready(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
        migration_table = await session.scalar(
            text("SELECT to_regclass('public.alembic_version')")
        )
        current_revisions = (
            frozenset(
                (
                    await session.execute(
                        text("SELECT version_num FROM public.alembic_version")
                    )
                )
                .scalars()
                .all()
            )
            if migration_table is not None
            else frozenset()
        )
    except Exception as exc:
        raise DomainError(
            "database_unavailable", "База данных недоступна.", status_code=503
        ) from exc
    if migration_table is None:
        raise DomainError("schema_pending", "Миграции ещё не применены.", status_code=503)
    expected_revisions = required_schema_revisions()
    if current_revisions != expected_revisions:
        raise DomainError(
            "schema_outdated",
            "Схема базы данных не соответствует версии приложения. Выполните миграции.",
            status_code=503,
            details={
                "current_revisions": sorted(current_revisions),
                "required_revisions": sorted(expected_revisions),
            },
        )
    return {"status": "ok"}


app.include_router(auth_router.router)
app.include_router(router)
