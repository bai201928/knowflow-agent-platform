"""Safety and determinism contract for the shared pytest harness.

This module is intentionally test-first for T025.  Database cleanup is exercised
only with an in-memory recorder; these tests must never migrate or clear a real
database.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Protocol

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import ArgumentError

import tests.conftest as harness_fixtures
from knowflow.api.dependencies import get_current_access_context
from knowflow.application.auth.policy import AccessContext
from knowflow.application.auth.service import AuthService
from knowflow.config import ModelMode, Settings
from knowflow.infrastructure.db.models.identity import RoleCode, User
from knowflow.infrastructure.models.adapters import ChatMessage, DeterministicModelStub
from tests.conftest import _clean_database, _safe_test_database_url

TEST_DATABASE_ENV = "KNOWFLOW_TEST_DATABASE_URL"
SAFE_DATABASE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]*_test$")

LEGAL_TEST_DATABASE_URLS = (
    "mysql+asyncmy://knowflow:test-only@localhost:3306/knowflow_test",
    "mysql+asyncmy://knowflow:test-only@127.0.0.1:3306/A1_test",
    "mysql+asyncmy://knowflow:safe-password-123@localhost/ordinary_test",
)

CONTROL_CHARACTER_URLS = tuple(
    pytest.param(
        f"mysql+{chr(code_point)}asyncmy://knowflow:test-only@127.0.0.1:3306/knowflow_test",
        id=f"raw-control-0x{code_point:02x}",
    )
    for code_point in (*range(0x20), 0x7F)
)

UNSAFE_TEST_DATABASE_URLS = (
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@prod.internal:3306/knowflow_test",
        id="remote-host",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@localhost.evil:3306/knowflow_test",
        id="localhost-subdomain",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@localhost.:3306/knowflow_test",
        id="localhost-trailing-dot",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@127.0.0.1.evil:3306/knowflow_test",
        id="ipv4-prefix-confusion",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@[::1]:3306/knowflow_test",
        id="ipv6-loopback-not-allowlisted",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@2130706433:3306/knowflow_test",
        id="integer-ipv4",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@127.0.0.1:3306/knowflow",
        id="missing-test-suffix",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@127.0.0.1:3306/prod%2Fknowflow_test",
        id="encoded-forward-slash",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@127.0.0.1:3306/prod%5Cknowflow_test",
        id="encoded-backslash",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@127.0.0.1:3306/prod%25knowflow_test",
        id="encoded-percent",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@127.0.0.1:3306/knowflow%5Ftest",
        id="encoded-underscore",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@127.0.0.1:3306/knowflow_test?charset=utf8mb4",
        id="benign-query-still-forbidden",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@127.0.0.1:3306/knowflow_test?",
        id="empty-query-delimiter",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@127.0.0.1:3306/knowflow_test?database=production",
        id="query-database-override",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@localhost/knowflow_test"
        "?unix_socket=/var/run/mysqld/mysqld.sock",
        id="mysql-unix-socket",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@127.0.0.1:3306/knowflow_test#ignored",
        id="fragment",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@127.0.0.1:3306/knowflow_test#",
        id="empty-fragment-delimiter",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:pass?word@127.0.0.1:3306/knowflow_test",
        id="query-delimiter-in-userinfo",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:pass#word@127.0.0.1:3306/knowflow_test",
        id="fragment-delimiter-in-userinfo",
    ),
    pytest.param(
        "mysql+asyncmy://127.0.0.1:3306/knowflow_test",
        id="missing-userinfo",
    ),
    pytest.param(
        "mysql+asyncmy://:test-only@127.0.0.1:3306/knowflow_test",
        id="empty-username",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow@127.0.0.1:3306/knowflow_test",
        id="missing-password",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:@127.0.0.1:3306/knowflow_test",
        id="empty-password",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@decoy@127.0.0.1:3306/knowflow_test",
        id="ambiguous-userinfo",
    ),
    pytest.param(
        "mysql+asyncmy://know%2Fflow:test-only@127.0.0.1:3306/knowflow_test",
        id="encoded-username",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@127.0.0.1:3306/_knowflow_test",
        id="database-leading-underscore",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@127.0.0.1:3306/know-flow_test",
        id="database-hyphen",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@127.0.0.1:3306/know.flow_test",
        id="database-dot",
    ),
    pytest.param(
        "mysql+asyncmy://knowflow:test-only@127.0.0.1:3306/知识_test",
        id="database-non-ascii",
    ),
    pytest.param(
        "mysql+pymysql://knowflow:test-only@127.0.0.1:3306/knowflow_test",
        id="wrong-driver",
    ),
    *CONTROL_CHARACTER_URLS,
)


class HarnessResponse(BaseModel):
    """Known structured value returned by the deterministic model fixture."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    answer: str


class MutableClock(Protocol):
    def now(self) -> datetime: ...

    def advance(self, **delta: float) -> None: ...


class SeededUser(Protocol):
    role: RoleCode
    user: User
    password: str
    context: AccessContext


class DatabaseCleaner(Protocol):
    async def __call__(self, database_url: str, session: Any) -> None: ...


class _RecordingSession:
    """Minimal async-session recorder that cannot contact a database."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.transaction_entries = 0

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        self.transaction_entries += 1
        yield

    async def execute(self, statement: object) -> None:
        self.statements.append(str(statement))


def _role(context: AccessContext) -> RoleCode:
    assert len(context.roles) == 1
    return next(iter(context.roles))


def test_settings_are_offline_sandboxed_and_bound_to_a_test_database(
    test_settings: Settings,
) -> None:
    assert test_settings.environment == "test"
    assert test_settings.model_mode is ModelMode.STUB
    assert test_settings.model_api_key is None
    assert test_settings.operations_sandbox_enabled is True
    assert test_settings.notification_sandbox_enabled is True
    assert test_settings.telemetry_export_enabled is False
    assert test_settings.database_dsn().rsplit("/", maxsplit=1)[-1].endswith("_test")


def test_clock_is_utc_and_advances_without_wall_clock_sleep(clock: MutableClock) -> None:
    before = clock.now()
    assert before.tzinfo is UTC

    clock.advance(seconds=37, microseconds=250)

    assert clock.now() == before + timedelta(seconds=37, microseconds=250)


async def test_deterministic_model_returns_reproducible_structure_and_embeddings(
    deterministic_model: DeterministicModelStub,
) -> None:
    deadline = 10**12
    first = await deterministic_model.chat_structured(
        messages=[ChatMessage(role="user", content="input must not change the fixture")],
        response_model=HarnessResponse,
        deadline=deadline,
    )
    second = await deterministic_model.chat_structured(
        messages=[ChatMessage(role="user", content="different input")],
        response_model=HarnessResponse,
        deadline=deadline,
    )
    first_vectors = await deterministic_model.embed(
        texts=["repeatable", "different", "repeatable"], deadline=deadline
    )
    second_vectors = await deterministic_model.embed(
        texts=["repeatable", "different", "repeatable"], deadline=deadline
    )

    assert first == HarnessResponse(schema_version="1", answer="deterministic")
    assert second == first
    assert first is not second
    assert first_vectors == second_vectors
    assert first_vectors[0] == first_vectors[2]
    assert first_vectors[0] != first_vectors[1]


def test_access_context_fixtures_cover_each_mvp_role(
    employee_context: AccessContext,
    operator_context: AccessContext,
    approver_context: AccessContext,
    admin_context: AccessContext,
) -> None:
    contexts = {
        RoleCode.EMPLOYEE: employee_context,
        RoleCode.OPERATOR: operator_context,
        RoleCode.APPROVER: approver_context,
        RoleCode.ADMIN: admin_context,
    }

    for expected_role, context in contexts.items():
        assert _role(context) is expected_role
        assert context.user_id
        assert context.session_id
        assert context.acl_version >= 1

    assert len({context.user_id for context in contexts.values()}) == 4


def test_app_uses_a_trusted_access_override_and_no_database_lifespan(
    app: Any,
    employee_context: AccessContext,
) -> None:
    assert get_current_access_context in app.dependency_overrides

    @app.get("/_test/whoami")
    async def whoami(
        context: Annotated[AccessContext, Depends(get_current_access_context)],
    ) -> dict[str, object]:
        return {"user_id": context.user_id, "roles": sorted(context.roles)}

    with TestClient(app) as client:
        response = client.get("/_test/whoami")

    assert response.status_code == 200
    assert response.json() == {
        "user_id": employee_context.user_id,
        "roles": [RoleCode.EMPLOYEE.value],
    }
    assert not hasattr(app.state, "database")


def test_app_fixture_is_function_scoped_and_starts_clean(
    app: Any, request: pytest.FixtureRequest
) -> None:
    fixture_definition = request._fixture_defs["app"]
    assert fixture_definition.scope == "function"
    assert "/_test/whoami" not in {route.path for route in app.routes}
    assert app.dependency_overrides == {
        get_current_access_context: app.dependency_overrides[get_current_access_context]
    }


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "mysql+asyncmy://knowflow:knowflow@127.0.0.1:3306/knowflow",
        "mysql+asyncmy://knowflow:knowflow@prod.internal:3306/knowflow_test",
        "mysql+asyncmy://knowflow:knowflow@127.0.0.1:3306/production",
    ],
)
def test_database_fixture_rejects_unsafe_or_non_test_urls(
    unsafe_url: str,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    monkeypatch.setenv(TEST_DATABASE_ENV, unsafe_url)

    with pytest.raises(pytest.UsageError, match="test database"):
        request.getfixturevalue("database")


def test_database_fixture_skips_when_explicit_test_url_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    monkeypatch.delenv(TEST_DATABASE_ENV, raising=False)

    with pytest.raises(pytest.skip.Exception, match=TEST_DATABASE_ENV):
        request.getfixturevalue("database")


@pytest.mark.parametrize("safe_url", LEGAL_TEST_DATABASE_URLS)
def test_database_url_guard_accepts_only_the_documented_mvp_shape(safe_url: str) -> None:
    parsed = _safe_test_database_url(safe_url)

    assert parsed.drivername == "mysql+asyncmy"
    assert parsed.host in {"localhost", "127.0.0.1"}
    assert parsed.query == {}
    assert parsed.database is not None
    assert SAFE_DATABASE_NAME.fullmatch(parsed.database)


@pytest.mark.parametrize("unsafe_url", UNSAFE_TEST_DATABASE_URLS)
def test_database_url_guard_rejects_every_ambiguous_or_redirectable_url(
    unsafe_url: str,
) -> None:
    with pytest.raises(pytest.UsageError, match="test database"):
        _safe_test_database_url(unsafe_url)


@pytest.mark.parametrize("unsafe_url", UNSAFE_TEST_DATABASE_URLS)
async def test_database_cleanup_rejects_dangerous_urls_before_transaction_or_sql(
    unsafe_url: str,
) -> None:
    recorder = _RecordingSession()

    with pytest.raises(pytest.UsageError, match="test database"):
        await _clean_database(unsafe_url, recorder)

    assert recorder.transaction_entries == 0
    assert recorder.statements == []


async def test_sqlalchemy_argument_error_is_normalized_before_transaction_or_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_url = LEGAL_TEST_DATABASE_URLS[0]
    recorder = _RecordingSession()

    def parser_failure(_: str) -> None:
        raise ArgumentError("synthetic parser detail must not escape")

    monkeypatch.setattr(harness_fixtures, "make_url", parser_failure)

    with pytest.raises(pytest.UsageError, match="test database"):
        _safe_test_database_url(safe_url)
    with pytest.raises(pytest.UsageError, match="test database"):
        await _clean_database(safe_url, recorder)

    assert recorder.transaction_entries == 0
    assert recorder.statements == []


async def test_database_cleanup_uses_ordered_deletes_inside_one_transaction(
    database_cleaner: DatabaseCleaner,
) -> None:
    recorder = _RecordingSession()
    await database_cleaner(
        "mysql+asyncmy://knowflow:test-only@127.0.0.1:3306/knowflow_test",
        recorder,
    )

    normalized = [statement.strip().upper() for statement in recorder.statements]
    assert recorder.transaction_entries == 1
    assert normalized
    assert all(statement.startswith("DELETE FROM ") for statement in normalized)
    assert not any("TRUNCATE" in statement or "DROP " in statement for statement in normalized)
    assert normalized.index("DELETE FROM USER_ROLES") < normalized.index("DELETE FROM USERS")
    assert normalized.index("DELETE FROM USERS") < normalized.index("DELETE FROM TEAMS")


async def test_database_cleanup_refuses_an_unsafe_url_before_executing_sql(
    database_cleaner: DatabaseCleaner,
) -> None:
    recorder = _RecordingSession()

    with pytest.raises(pytest.UsageError, match="test database"):
        await database_cleaner(
            "mysql+asyncmy://knowflow:knowflow@127.0.0.1:3306/knowflow",
            recorder,
        )

    assert recorder.transaction_entries == 0
    assert recorder.statements == []


def test_seeded_users_use_argon2_and_cover_exactly_four_roles(
    seeded_users: Mapping[RoleCode, SeededUser],
    test_settings: Settings,
) -> None:
    assert set(seeded_users) == set(RoleCode)
    auth = AuthService(test_settings)

    for role, seeded in seeded_users.items():
        assert seeded.role is role
        assert seeded.user.password_hash.startswith("$argon2")
        assert auth.verify_password(seeded.password, seeded.user.password_hash) is True
        assert seeded.password not in seeded.user.password_hash
        assert seeded.context.user_id == seeded.user.id
        assert seeded.context.roles == frozenset({role})


def test_harness_clock_has_a_stable_documented_epoch(clock: MutableClock) -> None:
    assert clock.now() == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
