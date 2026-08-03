from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from math import isfinite
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp

from knowflow.application.auth.policy import AccessContext
from knowflow.application.auth.service import AuthService, InvalidTokenError, TrustedClaims
from knowflow.config import Settings, get_settings
from knowflow.domain.common.deadlines import Deadline
from knowflow.domain.common.errors import ErrorCode, KnowFlowError
from knowflow.infrastructure.db.models.identity import (
    LoginSession,
    LoginSessionStatus,
    PrincipalStatus,
    Role,
    RoleCode,
    User,
    UserRole,
)
from knowflow.infrastructure.db.session import Database

_SAFE_CORRELATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_bearer = HTTPBearer(auto_error=False)

HealthProbe = Callable[[], Awaitable[bool]]


def _safe_header_id(value: str | None) -> str:
    if value is not None and _SAFE_CORRELATION_ID.fullmatch(value):
        return value
    return str(uuid4())


def _deadline_seconds(request: Request, settings: Settings) -> float:
    value = request.headers.get("X-Interaction-Deadline-Seconds")
    if value is None:
        return settings.default_interaction_deadline_seconds
    try:
        seconds = float(value)
    except ValueError as exc:
        raise KnowFlowError(
            ErrorCode.VALIDATION_FAILED,
            "X-Interaction-Deadline-Seconds must be a number",
            status=422,
        ) from exc
    if not isfinite(seconds) or seconds <= 0 or seconds > 60:
        raise KnowFlowError(
            ErrorCode.VALIDATION_FAILED,
            "X-Interaction-Deadline-Seconds must be greater than zero and at most 60",
            status=422,
        )
    return seconds


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Create correlation facts and one monotonic deadline at HTTP ingress."""

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = _safe_header_id(request.headers.get("X-Request-ID"))
        trace_id = _safe_header_id(request.headers.get("X-Trace-ID"))
        request.state.request_id = request_id
        request.state.trace_id = trace_id
        try:
            request.state.deadline = Deadline.after(_deadline_seconds(request, self._settings))
        except KnowFlowError as exc:
            from knowflow.api.error_handlers import knowflow_error_handler

            error_response = await knowflow_error_handler(request, exc)
            error_response.headers["X-Request-ID"] = request_id
            error_response.headers["X-Trace-ID"] = trace_id
            return error_response
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Trace-ID"] = trace_id
        return response


def get_request_id(request: Request) -> str:
    return str(request.state.request_id)


def get_trace_id(request: Request) -> str:
    return str(request.state.trace_id)


def get_deadline(request: Request) -> Deadline:
    deadline = request.state.deadline
    if not isinstance(deadline, Deadline):
        raise RuntimeError("request deadline middleware is not installed")
    return deadline


def get_database(request: Request) -> Database:
    database = getattr(request.app.state, "database", None)
    if not isinstance(database, Database):
        raise KnowFlowError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "Database dependency is unavailable",
            status=503,
            retryable=True,
        )
    return database


async def get_db_session(
    database: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    async with database.session() as session:
        yield session


def get_auth_service(request: Request) -> AuthService:
    service = getattr(request.app.state, "auth_service", None)
    if isinstance(service, AuthService):
        return service
    return AuthService(get_settings())


def _authentication_error(detail: str = "Authentication is required") -> KnowFlowError:
    return KnowFlowError(ErrorCode.AUTHENTICATION_REQUIRED, detail, status=401)


async def decode_bearer_claims(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> TrustedClaims:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _authentication_error()
    try:
        return service.decode_access_token(credentials.credentials)
    except InvalidTokenError as exc:
        raise _authentication_error("Access token is invalid or expired") from exc


async def get_current_access_context(
    claims: Annotated[TrustedClaims, Depends(decode_bearer_claims)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AccessContext:
    """Re-resolve mutable authorization state instead of trusting token role claims."""

    identity = await session.execute(
        select(LoginSession, User)
        .join(User, User.id == LoginSession.user_id)
        .where(LoginSession.id == claims.session_id, User.id == claims.user_id)
    )
    row = identity.one_or_none()
    if row is None:
        raise _authentication_error("Session is no longer valid")
    login_session, user = row.tuple()
    now = datetime.now(UTC)
    if (
        login_session.status != LoginSessionStatus.ACTIVE
        or login_session.expires_at <= now
        or user.status != PrincipalStatus.ACTIVE
    ):
        raise _authentication_error("Session is no longer active")

    role_result = await session.execute(
        select(Role.code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user.id)
    )
    roles = frozenset(role_result.scalars().all())
    if not roles:
        raise _authentication_error("User has no active roles")
    if user.acl_version != claims.acl_version or roles != frozenset(claims.roles):
        raise _authentication_error("Access permissions changed; authenticate again")
    return AccessContext(
        user_id=user.id,
        session_id=login_session.id,
        roles=frozenset(RoleCode(role) for role in roles),
        team_id=user.team_id,
        acl_version=user.acl_version,
    )


CurrentAccessContext = Annotated[AccessContext, Depends(get_current_access_context)]
