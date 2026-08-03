from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from knowflow.api.dependencies import decode_bearer_claims, get_current_access_context
from knowflow.api.main import create_app
from knowflow.application.auth.service import TrustedClaims
from knowflow.config import Settings
from knowflow.infrastructure.db.models.identity import (
    LoginSession,
    LoginSessionStatus,
    PrincipalStatus,
    RoleCode,
    User,
)

NOW = datetime.now(UTC)


@asynccontextmanager
async def _no_lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield


def _settings() -> Settings:
    return Settings(jwt_secret="security-test-secret-that-is-at-least-32-characters")


def _claims(
    *,
    roles: tuple[RoleCode, ...] = (RoleCode.EMPLOYEE,),
    acl_version: int = 3,
) -> TrustedClaims:
    return TrustedClaims(
        user_id="user-1",
        session_id="session-1",
        roles=roles,
        acl_version=acl_version,
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=10),
        token_id="token-1",
    )


def _identity(
    *,
    session_status: LoginSessionStatus = LoginSessionStatus.ACTIVE,
    session_expiry: datetime | None = None,
    user_status: PrincipalStatus = PrincipalStatus.ACTIVE,
    acl_version: int = 3,
) -> tuple[LoginSession, User]:
    login_session = LoginSession(
        id="session-1",
        user_id="user-1",
        token_family_id="family-1",
        status=session_status,
        expires_at=session_expiry or NOW + timedelta(minutes=10),
    )
    user = User(
        id="user-1",
        username="employee",
        password_hash="unused-test-hash",
        display_name="Employee",
        status=user_status,
        team_id="team-1",
        acl_version=acl_version,
    )
    return login_session, user


def _db_session(
    identity: tuple[LoginSession, User], roles: list[RoleCode]
) -> AsyncMock:
    identity_row = MagicMock()
    identity_row.tuple.return_value = identity
    identity_result = MagicMock()
    identity_result.one_or_none.return_value = identity_row
    role_result = MagicMock()
    role_result.scalars.return_value.all.return_value = roles
    session = AsyncMock()
    session.execute.side_effect = [identity_result, role_result]
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_status", "session_expiry", "user_status"),
    [
        (LoginSessionStatus.REVOKED, NOW + timedelta(minutes=10), PrincipalStatus.ACTIVE),
        (LoginSessionStatus.EXPIRED, NOW + timedelta(minutes=10), PrincipalStatus.ACTIVE),
        (LoginSessionStatus.ACTIVE, NOW - timedelta(seconds=1), PrincipalStatus.ACTIVE),
        (LoginSessionStatus.ACTIVE, NOW + timedelta(minutes=10), PrincipalStatus.DISABLED),
        (LoginSessionStatus.ACTIVE, NOW + timedelta(minutes=10), PrincipalStatus.LOCKED),
    ],
)
async def test_mutated_session_or_principal_state_rejects_an_old_token(
    session_status: LoginSessionStatus,
    session_expiry: datetime,
    user_status: PrincipalStatus,
) -> None:
    identity = _identity(
        session_status=session_status,
        session_expiry=session_expiry,
        user_status=user_status,
    )
    with pytest.raises(Exception, match="Session is no longer active") as captured:
        await get_current_access_context(_claims(), _db_session(identity, [RoleCode.EMPLOYEE]))
    assert getattr(captured.value, "status", None) == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claims", "database_roles", "database_acl"),
    [
        (_claims(roles=(RoleCode.ADMIN,)), [RoleCode.EMPLOYEE], 3),
        (_claims(acl_version=2), [RoleCode.EMPLOYEE], 3),
        (_claims(roles=(RoleCode.EMPLOYEE,)), [RoleCode.OPERATOR], 3),
    ],
)
async def test_forged_or_stale_role_and_acl_claims_are_not_authoritative(
    claims: TrustedClaims,
    database_roles: list[RoleCode],
    database_acl: int,
) -> None:
    identity = _identity(acl_version=database_acl)
    with pytest.raises(Exception, match="Access permissions changed") as captured:
        await get_current_access_context(claims, _db_session(identity, database_roles))
    assert getattr(captured.value, "status", None) == 401


def test_missing_bearer_credentials_use_a_non_disclosing_problem_response() -> None:
    app = create_app(settings=_settings(), lifespan=_no_lifespan)

    @app.get("/protected")
    async def protected(
        _: Annotated[Any, Depends(decode_bearer_claims)],
    ) -> dict[str, bool]:
        return {"authenticated": True}

    with TestClient(app) as client:
        response = client.get("/protected", headers={"X-Request-ID": "auth-request-1"})
    body = response.json()
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["content-type"].startswith("application/problem+json")
    assert body["code"] == "AUTHENTICATION_REQUIRED"
    assert body["request_id"] == "auth-request-1"
    assert "secret" not in response.text.lower()


@pytest.mark.parametrize("value", ["0", "-1", "61", "inf", "-inf", "nan", "not-a-number"])
def test_invalid_deadline_header_is_rejected_as_problem_json(value: str) -> None:
    app = create_app(settings=_settings(), lifespan=_no_lifespan)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/health/live",
            headers={
                "X-Interaction-Deadline-Seconds": value,
                "X-Request-ID": "deadline-request-1",
            },
        )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["request_id"] == "deadline-request-1"


@pytest.mark.parametrize(
    "header",
    ["has spaces", "line/tab", "../traversal", "x" * 65, "<script>alert(1)</script>"],
)
def test_untrusted_correlation_headers_are_never_reflected(header: str) -> None:
    app = create_app(settings=_settings(), lifespan=_no_lifespan)
    with TestClient(app) as client:
        response = client.get(
            "/health/live",
            headers={"X-Request-ID": header, "X-Trace-ID": header},
        )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != header
    assert response.headers["X-Trace-ID"] != header


def test_readiness_contains_probe_failure_without_disclosing_exception() -> None:
    app = create_app(settings=_settings(), lifespan=_no_lifespan)

    async def broken_probe() -> bool:
        raise RuntimeError("mysql://root:password@private-host/knowflow")

    app.state.health_probes = {"mysql": broken_probe}
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "dependencies": {"mysql": "unavailable"},
    }
    assert "password" not in response.text


def test_unexpected_error_is_rfc_9457_problem_with_request_id_and_no_leak() -> None:
    app = create_app(settings=_settings(), lifespan=_no_lifespan)

    @app.get("/unexpected")
    async def unexpected() -> None:
        raise RuntimeError("database password=top-secret")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/unexpected", headers={"X-Request-ID": "unexpected-request-1"})
    body = response.json()
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert body["code"] == "INTERNAL_ERROR"
    assert body["request_id"] == "unexpected-request-1"
    assert "top-secret" not in response.text


def test_runtime_openapi_is_31_and_matches_application_version() -> None:
    app = create_app(settings=_settings(), lifespan=_no_lifespan)
    with TestClient(app) as client:
        document = client.get("/openapi.json").json()
    assert document["openapi"].startswith("3.1.")
    assert document["info"]["version"] == "0.2.0"
