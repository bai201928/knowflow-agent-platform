from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from knowflow.domain.common.errors import ErrorCode, KnowFlowError

PROBLEM_MEDIA_TYPE = "application/problem+json"


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return str(value) if value else "unavailable"


def _problem_response(
    problem: dict[str, Any], *, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=int(problem["status"]),
        content=problem,
        headers=headers,
        media_type=PROBLEM_MEDIA_TYPE,
    )


async def knowflow_error_handler(request: Request, exc: KnowFlowError) -> JSONResponse:
    current_version = exc.context.get("current_version")
    problem = exc.to_problem(
        request_id=_request_id(request),
        current_version=current_version if isinstance(current_version, int) else None,
    )
    headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
    return _problem_response(problem, headers=headers)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    fields: list[dict[str, str]] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        fields.append({"field": location, "message": str(error.get("msg", "Invalid value"))})
    problem = KnowFlowError(
        ErrorCode.VALIDATION_FAILED,
        "Request validation failed",
        status=422,
    ).to_problem(request_id=_request_id(request), fields=fields)
    return _problem_response(problem)


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = (
        ErrorCode.RESOURCE_NOT_FOUND
        if exc.status_code == 404
        else ErrorCode.AUTHENTICATION_REQUIRED
        if exc.status_code == 401
        else ErrorCode.PERMISSION_DENIED
        if exc.status_code == 403
        else ErrorCode.VALIDATION_FAILED
        if exc.status_code in {400, 405}
        else ErrorCode.INTERNAL_ERROR
    )
    detail = exc.detail if isinstance(exc.detail, str) else None
    problem = KnowFlowError(code, detail, status=exc.status_code).to_problem(
        request_id=_request_id(request)
    )
    return _problem_response(problem, headers=exc.headers)


async def permission_error_handler(request: Request, exc: PermissionError) -> JSONResponse:
    del exc
    problem = KnowFlowError(
        ErrorCode.PERMISSION_DENIED,
        "The requested action is not permitted",
        status=403,
    ).to_problem(request_id=_request_id(request))
    return _problem_response(problem)


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    del exc
    problem = KnowFlowError(
        ErrorCode.INTERNAL_ERROR,
        "An unexpected error occurred",
        status=500,
    ).to_problem(request_id=_request_id(request))
    return _problem_response(problem)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(KnowFlowError, knowflow_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(PermissionError, permission_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unexpected_error_handler)
