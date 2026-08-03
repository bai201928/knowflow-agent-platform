"""Shared, deterministic, and fail-closed pytest fixtures for KnowFlow.

The database fixtures deliberately require a dedicated environment variable and
validate its target before an engine or SQL statement can be created.  Unit tests
therefore stay offline by default, while integration tests must opt in to a local
database whose name ends in ``_test``.
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlsplit

import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncSession

from knowflow.api.dependencies import get_current_access_context
from knowflow.api.main import create_app
from knowflow.application.auth.policy import AccessContext
from knowflow.application.auth.service import AuthService
from knowflow.config import ModelMode, Settings
from knowflow.infrastructure.db.models import Base
from knowflow.infrastructure.db.models.identity import RoleCode, User
from knowflow.infrastructure.db.session import Database
from knowflow.infrastructure.models.adapters import DeterministicModelStub

TEST_DATABASE_ENV = "KNOWFLOW_TEST_DATABASE_URL"
TEST_DATABASE_HOSTS = frozenset({"127.0.0.1", "localhost"})
TEST_EPOCH = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
TEST_DATABASE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]*_test$", re.ASCII)
UNAMBIGUOUS_USERINFO_PART = re.compile(r"^[A-Za-z0-9._~!$&'()*+,;=-]+$", re.ASCII)

# Children are deleted before parents.  This explicit list also makes cleanup
# reviewable; no test helper discovers arbitrary table names from a live server.
DELETE_ORDER = (
    "audit_events",
    "retrieval_evidence",
    "evaluation_results",
    "evaluation_runs",
    "notification_deliveries",
    "ticket_events",
    "approvals",
    "plan_dependencies",
    "plan_tasks",
    "workflow_commands",
    "outbox_events",
    "inbox_messages",
    "workflow_plans",
    "workflows",
    "tickets",
    "document_segments",
    "document_versions",
    "document_acl_grants",
    "documents",
    "operation_ledger",
    "login_sessions",
    "user_roles",
    "users",
    "roles",
    "teams",
)
DELETE_TABLES = tuple(Base.metadata.tables[name] for name in DELETE_ORDER)


@dataclass(slots=True)
class AdvancingClock:
    """Small UTC clock double; advancing it never sleeps or reads wall time."""

    current: datetime = TEST_EPOCH

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> None:
        self.current += timedelta(**delta)


@dataclass(frozen=True, slots=True)
class SeededUser:
    """In-memory identity material used by authentication and policy tests."""

    role: RoleCode
    user: User
    password: str
    context: AccessContext


class _HarnessResponse(BaseModel):
    schema_version: str
    answer: str


def _safe_test_database_url(raw_url: str) -> URL:
    """Parse and enforce the non-negotiable local test-database boundary."""

    error_message = (
        f"{TEST_DATABASE_ENV} must identify an unambiguous mysql+asyncmy test database "
        "on exact host localhost/127.0.0.1 whose ASCII name ends in _test"
    )

    try:
        if (
            raw_url != raw_url.strip()
            or "?" in raw_url
            or "#" in raw_url
            or any(ord(character) <= 0x1F or ord(character) == 0x7F for character in raw_url)
        ):
            raise ValueError("raw URL contains a forbidden delimiter or control character")
        split_url = urlsplit(raw_url)
        if (
            split_url.scheme != "mysql+asyncmy"
            or split_url.query
            or split_url.fragment
            or split_url.netloc.count("@") != 1
        ):
            raise ValueError("unsafe URL envelope")

        raw_userinfo, raw_hostport = split_url.netloc.split("@", maxsplit=1)
        if raw_userinfo.count(":") != 1 or "%" in raw_userinfo:
            raise ValueError("ambiguous or encoded userinfo")
        raw_username, raw_password = raw_userinfo.split(":", maxsplit=1)
        if not (
            UNAMBIGUOUS_USERINFO_PART.fullmatch(raw_username)
            and UNAMBIGUOUS_USERINFO_PART.fullmatch(raw_password)
        ):
            raise ValueError("missing or ambiguous credentials")

        if raw_hostport.count(":") > 1 or "[" in raw_hostport or "]" in raw_hostport:
            raise ValueError("ambiguous host")
        raw_host, port_separator, raw_port = raw_hostport.partition(":")
        if raw_host not in TEST_DATABASE_HOSTS:
            raise ValueError("host is not allowlisted")
        if port_separator and (
            not raw_port.isascii() or not raw_port.isdecimal() or not 1 <= int(raw_port) <= 65535
        ):
            raise ValueError("invalid port")

        raw_path = split_url.path
        if (
            not raw_path.startswith("/")
            or raw_path.count("/") != 1
            or "%" in raw_path
            or unquote(raw_path) != raw_path
        ):
            raise ValueError("encoded or ambiguous database path")
        raw_database_name = raw_path[1:]
        if TEST_DATABASE_NAME.fullmatch(raw_database_name) is None:
            raise ValueError("unsafe database name")

        url = make_url(raw_url)
        if (
            url.drivername != split_url.scheme
            or url.username != raw_username
            or url.password != raw_password
            or url.host != raw_host
            or url.database != raw_database_name
            or bool(url.query)
        ):
            raise ValueError("parser disagreement")
    except (ArgumentError, TypeError, ValueError) as exc:
        raise pytest.UsageError(error_message) from exc
    return url


def _required_test_database_url() -> URL:
    raw_url = os.getenv(TEST_DATABASE_ENV)
    if raw_url is None or not raw_url.strip():
        pytest.skip(f"set {TEST_DATABASE_ENV} explicitly to run database-backed tests")
    return _safe_test_database_url(raw_url)


@pytest.fixture
def test_settings() -> Settings:
    """Offline settings with every external side-effect boundary sandboxed."""

    return Settings(
        environment="test",
        database_url="mysql+asyncmy://knowflow:test-only@127.0.0.1:3306/knowflow_test",
        jwt_secret="knowflow-test-jwt-secret-at-least-32-characters",
        model_mode=ModelMode.STUB,
        model_api_key=None,
        operations_sandbox_enabled=True,
        notification_sandbox_enabled=True,
        telemetry_export_enabled=False,
    )


@pytest.fixture
def clock() -> AdvancingClock:
    return AdvancingClock()


@pytest.fixture
def deterministic_model() -> DeterministicModelStub:
    return DeterministicModelStub(
        structured_response=_HarnessResponse(schema_version="1", answer="deterministic"),
        embedding_dimensions=8,
    )


def _access_context(role: RoleCode) -> AccessContext:
    suffix = role.value.lower()
    return AccessContext(
        user_id=f"test-user-{suffix}",
        session_id=f"test-session-{suffix}",
        roles=frozenset({role}),
        team_id="test-team-operations" if role is RoleCode.OPERATOR else None,
        acl_version=1,
    )


@pytest.fixture
def employee_context() -> AccessContext:
    return _access_context(RoleCode.EMPLOYEE)


@pytest.fixture
def operator_context() -> AccessContext:
    return _access_context(RoleCode.OPERATOR)


@pytest.fixture
def approver_context() -> AccessContext:
    return _access_context(RoleCode.APPROVER)


@pytest.fixture
def admin_context() -> AccessContext:
    return _access_context(RoleCode.ADMIN)


@pytest.fixture
def app(test_settings: Settings, employee_context: AccessContext) -> FastAPI:
    """Fresh API app with no production lifespan and one trusted identity override."""

    @asynccontextmanager
    async def no_external_lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield

    async def trusted_employee() -> AccessContext:
        return employee_context

    test_app = create_app(settings=test_settings, lifespan=no_external_lifespan)
    # FastAPI 0.139 keeps included routers as lazy, path-less internal objects.
    # This focused fixture does not need production health routes, and removing
    # their wrapper gives tests a conventional, directly inspectable route list.
    test_app.router.routes[:] = [
        route for route in test_app.router.routes if hasattr(route, "path")
    ]
    test_app.dependency_overrides[get_current_access_context] = trusted_employee
    return test_app


@pytest.fixture
def database() -> Database:
    """Opt-in local test database; validation occurs before engine construction."""

    url = _required_test_database_url()
    settings = Settings(
        environment="test",
        database_url=url.render_as_string(hide_password=False),
        jwt_secret="knowflow-test-jwt-secret-at-least-32-characters",
        model_mode=ModelMode.STUB,
    )
    return Database(settings)


@pytest.fixture
def database_cleaner() -> Callable[[str, Any], Awaitable[None]]:
    """Return a guarded, transactional, DELETE-only database cleaner."""

    return _clean_database


async def _clean_database(database_url: str, session: Any) -> None:
    _safe_test_database_url(database_url)
    async with session.begin():
        for table in DELETE_TABLES:
            await session.execute(table.delete())


@pytest_asyncio.fixture
async def database_session(database: Database) -> AsyncIterator[AsyncSession]:
    """Clean an explicitly selected local test database around each integration test."""

    try:
        async with database.session() as session:
            database_url = database.engine.url.render_as_string(hide_password=False)
            await _clean_database(database_url, session)
            yield session
            await _clean_database(database_url, session)
    finally:
        await database.close()


@pytest.fixture
def seeded_users(
    test_settings: Settings,
    employee_context: AccessContext,
    operator_context: AccessContext,
    approver_context: AccessContext,
    admin_context: AccessContext,
) -> Mapping[RoleCode, SeededUser]:
    """Four deterministic principals with production-equivalent Argon2 password hashes."""

    auth = AuthService(test_settings)
    contexts = {
        RoleCode.EMPLOYEE: employee_context,
        RoleCode.OPERATOR: operator_context,
        RoleCode.APPROVER: approver_context,
        RoleCode.ADMIN: admin_context,
    }
    seeded: dict[RoleCode, SeededUser] = {}
    for role, context in contexts.items():
        password = f"KnowFlow-{role.value}-test-password"
        user = User(
            id=context.user_id,
            username=f"test-{role.value.lower()}",
            password_hash=auth.hash_password(password),
            display_name=f"Test {role.value.title()}",
            team_id=context.team_id,
            acl_version=context.acl_version,
        )
        seeded[role] = SeededUser(
            role=role,
            user=user,
            password=password,
            context=context,
        )
    return seeded
