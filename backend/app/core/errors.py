from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class ApiError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str


class DomainError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 422,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    error = ApiError(
        code=exc.code,
        message=exc.message,
        details=exc.details,
        request_id=request_id,
    )
    return JSONResponse(status_code=exc.status_code, content=error.model_dump(mode="json"))


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    details = {
        "errors": [
            {
                "location": [str(part) for part in item["loc"]],
                "message": item["msg"],
                "type": item["type"],
            }
            for item in exc.errors()
        ]
    }
    error = ApiError(
        code="validation_error",
        message="Входные данные не прошли проверку.",
        details=details,
        request_id=request_id,
    )
    return JSONResponse(status_code=422, content=error.model_dump(mode="json"))
