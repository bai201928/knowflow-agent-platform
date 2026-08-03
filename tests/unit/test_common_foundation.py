from __future__ import annotations

import json
import time
import uuid

import pytest

from knowflow.config import ModelMode, Settings
from knowflow.domain.common.deadlines import Deadline, RetryPolicy
from knowflow.domain.common.errors import ErrorCode, KnowFlowError
from knowflow.domain.common.identity import canonical_json, operation_id, payload_hash


def test_canonical_identity_is_order_independent_and_payload_sensitive() -> None:
    left = {"ticket": {"severity": "P1", "title": "Backlog"}, "recipients": ["ops"]}
    same = {"recipients": ["ops"], "ticket": {"title": "Backlog", "severity": "P1"}}
    changed = {"recipients": ["ops"], "ticket": {"title": "Backlog", "severity": "P2"}}

    assert canonical_json(left) == canonical_json(same)
    assert payload_hash(left) == payload_hash(same)
    assert payload_hash(left) != payload_hash(changed)
    assert operation_id("workflow-1", "ticket-create", left) == operation_id(
        "workflow-1", "ticket-create", same
    )


def test_operation_identity_is_a_stable_uuid5() -> None:
    value = operation_id("workflow-1", "notify", {"recipient": "on-call"})
    assert isinstance(value, uuid.UUID)
    assert value.version == 5


def test_problem_details_are_stable_and_do_not_leak_internal_context() -> None:
    error = KnowFlowError(
        ErrorCode.VERSION_CONFLICT,
        "The resource changed",
        status=409,
        retryable=True,
        context={"database_password": "must-not-leak"},
    )
    problem = error.to_problem(request_id="req-1", current_version=3)

    assert problem["code"] == "VERSION_CONFLICT"
    assert problem["request_id"] == "req-1"
    assert problem["current_version"] == 3
    assert "must-not-leak" not in json.dumps(problem)


def test_deadline_budget_and_retry_policy_are_bounded() -> None:
    deadline = Deadline.after(5)
    assert 0 < deadline.remaining_seconds() <= 5
    assert deadline.timeout_seconds(maximum=1.5) <= 1.5

    policy = RetryPolicy(max_attempts=3, base_delay_seconds=0.1, max_delay_seconds=0.25)
    assert policy.delay_for_attempt(1) == pytest.approx(0.1)
    assert policy.delay_for_attempt(3) == pytest.approx(0.25)
    with pytest.raises(ValueError):
        policy.delay_for_attempt(4)


def test_deadline_never_extends_past_absolute_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    deadline = Deadline.at(100.05)
    with pytest.raises(KnowFlowError) as captured:
        deadline.timeout_seconds(maximum=1.0, minimum=0.1)
    assert captured.value.code is ErrorCode.DEADLINE_EXCEEDED


def test_retry_requires_enough_budget_for_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)
    policy = RetryPolicy(max_attempts=3, base_delay_seconds=0.25, max_delay_seconds=1.0)
    assert policy.allows_retry(attempt=1, safe_to_retry=True, deadline=Deadline.at(100.30))
    assert not policy.allows_retry(attempt=1, safe_to_retry=True, deadline=Deadline.at(100.20))


def test_real_model_mode_requires_provider_secret() -> None:
    with pytest.raises(ValueError, match="MODEL_API_KEY"):
        Settings(model_mode=ModelMode.REAL, jwt_secret="local-test-secret-at-least-32-characters")


def test_stub_mode_has_safe_local_defaults() -> None:
    settings = Settings(jwt_secret="local-test-secret-at-least-32-characters")
    assert settings.model_mode is ModelMode.STUB
    assert settings.operations_sandbox_enabled is True
    assert settings.notification_sandbox_enabled is True


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({}, "JWT secret"),
        (
            {"jwt_secret": "production-secret-that-is-at-least-32-characters"},
            "database URL",
        ),
        (
            {
                "jwt_secret": "production-secret-that-is-at-least-32-characters",
                "database_url": "mysql+asyncmy://app:secret@database.internal/knowflow",
            },
            "Redis URL",
        ),
    ],
)
def test_nonlocal_mode_rejects_default_credentials(overrides: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Settings(environment="production", **overrides)


@pytest.mark.parametrize(
    "sandbox_field", ["operations_sandbox_enabled", "notification_sandbox_enabled"]
)
def test_nonlocal_mode_cannot_disable_sandbox(sandbox_field: str) -> None:
    overrides = {
        "environment": "production",
        "jwt_secret": "production-secret-that-is-at-least-32-characters",
        "database_url": "mysql+asyncmy://app:secret@database.internal/knowflow",
        "redis_url": "redis://:secret@redis.internal/0",
        sandbox_field: False,
    }
    with pytest.raises(ValueError, match="sandbox"):
        Settings(**overrides)
