"""T036 red tests for the durable operation-ledger application service.

These tests deliberately use an in-memory repository and unit of work.  They
specify transaction and concurrency semantics without depending on SQLAlchemy,
MySQL, wall-clock sleeps, or a ticket implementation that belongs to T037.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from knowflow.application.workflows.operations import (
    MAX_OPERATION_SUMMARY_CHARS,
    EffectResult,
    LedgerEntry,
    OperationEffectError,
    OperationLedgerService,
    OperationRepository,
    OperationRequest,
    OperationUnitOfWork,
)
from knowflow.domain.common.errors import ErrorCode, KnowFlowError
from knowflow.infrastructure.db.models.workflow import OperationStatus

OPERATION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PAYLOAD_HASH = "a" * 64
OTHER_PAYLOAD_HASH = "b" * 64
STARTED_AT = datetime(2026, 8, 3, 1, 0, tzinfo=UTC)


class MutableClock:
    def __init__(self, current: datetime = STARTED_AT) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> None:
        self.current += timedelta(**delta)


class MemoryState:
    def __init__(self) -> None:
        self.entries: dict[str, LedgerEntry] = {}
        self.ticket_results: dict[str, dict[str, Any]] = {}
        self.lock = asyncio.Lock()
        self.effect_calls = 0


class MemoryOperationRepository:
    def __init__(self, state: MemoryState) -> None:
        self._state = state

    async def get_for_update(self, operation_id: str) -> LedgerEntry | None:
        return self._state.entries.get(operation_id)

    async def add(self, entry: LedgerEntry) -> None:
        if entry.operation_id in self._state.entries:
            raise RuntimeError("duplicate operation identity")
        self._state.entries[entry.operation_id] = entry

    async def save(self, entry: LedgerEntry) -> None:
        if entry.operation_id not in self._state.entries:
            raise RuntimeError("operation identity does not exist")
        self._state.entries[entry.operation_id] = entry


class MemoryUnitOfWork:
    """Serialisable fake with rollback snapshots for deterministic race tests."""

    def __init__(self, state: MemoryState) -> None:
        self.state = state
        self.operations: OperationRepository = MemoryOperationRepository(state)
        self._entries_before: dict[str, LedgerEntry] = {}
        self._tickets_before: dict[str, dict[str, Any]] = {}
        self._committed = False

    async def __aenter__(self) -> MemoryUnitOfWork:
        await self.state.lock.acquire()
        self._entries_before = deepcopy(self.state.entries)
        self._tickets_before = deepcopy(self.state.ticket_results)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc, traceback
        if exc_type is not None or not self._committed:
            self.state.entries = self._entries_before
            self.state.ticket_results = self._tickets_before
        self.state.lock.release()

    async def commit(self) -> None:
        self._committed = True

    async def rollback(self) -> None:
        self.state.entries = deepcopy(self._entries_before)
        self.state.ticket_results = deepcopy(self._tickets_before)
        self._committed = False


class MemoryUowFactory:
    def __init__(self, state: MemoryState) -> None:
        self.state = state

    def __call__(self) -> OperationUnitOfWork:
        return MemoryUnitOfWork(self.state)


def _request(
    *, payload_hash: str = PAYLOAD_HASH, operation_id: UUID = OPERATION_ID
) -> OperationRequest:
    return OperationRequest(
        operation_id=str(operation_id),
        scope_type="workflow",
        scope_id="11111111-1111-4111-8111-111111111111",
        operation_type="ticket.create",
        payload_hash=payload_hash,
    )


def _service(
    state: MemoryState,
    clock: MutableClock,
    *,
    lease_duration: timedelta = timedelta(seconds=30),
) -> OperationLedgerService:
    factory: Callable[[], OperationUnitOfWork] = MemoryUowFactory(state)
    return OperationLedgerService(
        uow_factory=factory,
        clock=clock.now,
        lease_duration=lease_duration,
    )


def _ticket_effect(
    state: MemoryState,
    *,
    ticket_id: str = "INC-1001",
) -> Callable[[OperationUnitOfWork], Any]:
    async def effect(uow: OperationUnitOfWork) -> EffectResult:
        assert isinstance(uow, MemoryUnitOfWork)
        state.effect_calls += 1
        uow.state.ticket_results[ticket_id] = {
            "id": ticket_id,
            "version": 1,
            "status": "OPEN",
        }
        await asyncio.sleep(0)
        return EffectResult(
            resource_type="ticket",
            resource_id=ticket_id,
            resource_version=1,
            result_summary={"ticket_id": ticket_id, "version": 1},
        )

    return effect


async def test_new_claim_and_same_hash_running_replay_do_not_increment_attempt() -> None:
    state = MemoryState()
    clock = MutableClock()
    service = _service(state, clock)

    first = await service.claim(_request(), lease_owner="worker-a")
    replay = await service.claim(_request(), lease_owner="worker-b")

    assert first.status is OperationStatus.RUNNING
    assert first.replayed is False
    assert first.attempt_count == 1
    assert replay.status is OperationStatus.RUNNING
    assert replay.replayed is True
    assert replay.attempt_count == 1
    assert state.entries[str(OPERATION_ID)].lease_owner == "worker-a"


async def test_same_operation_identity_with_different_payload_is_a_safe_conflict() -> None:
    state = MemoryState()
    service = _service(state, MutableClock())
    await service.claim(_request(), lease_owner="worker-a")

    with pytest.raises(KnowFlowError) as caught:
        await service.claim(
            _request(payload_hash=OTHER_PAYLOAD_HASH),
            lease_owner="worker-b",
        )

    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert caught.value.retryable is False
    assert PAYLOAD_HASH not in str(caught.value)
    assert OTHER_PAYLOAD_HASH not in str(caught.value)
    assert state.entries[str(OPERATION_ID)].attempt_count == 1


async def test_only_an_expired_running_lease_can_be_taken_over() -> None:
    state = MemoryState()
    clock = MutableClock()
    service = _service(state, clock)
    initial = await service.claim(_request(), lease_owner="worker-a")

    clock.advance(seconds=29)
    still_owned = await service.claim(_request(), lease_owner="worker-b")
    assert still_owned.replayed is True
    assert still_owned.attempt_count == 1

    clock.advance(seconds=2)
    takeover = await service.claim(_request(), lease_owner="worker-b")
    assert takeover.replayed is False
    assert takeover.status is OperationStatus.RUNNING
    assert takeover.attempt_count == 2
    assert state.entries[str(OPERATION_ID)].lease_owner == "worker-b"
    current_lease_until = state.entries[str(OPERATION_ID)].lease_until
    assert initial.lease_until is not None
    assert current_lease_until is not None
    assert current_lease_until > initial.lease_until


async def test_retryable_failure_can_be_reclaimed_but_attempt_count_is_monotonic() -> None:
    state = MemoryState()
    service = _service(state, MutableClock())
    request = _request()
    await service.claim(request, lease_owner="worker-a")

    failed = await service.mark_failed(
        request,
        lease_owner="worker-a",
        error_code="DEPENDENCY_TIMEOUT",
        error_summary="provider timed out",
        retryable=True,
    )
    retried = await service.claim(request, lease_owner="worker-b")

    assert failed.status is OperationStatus.FAILED_RETRYABLE
    assert failed.attempt_count == 1
    assert retried.status is OperationStatus.RUNNING
    assert retried.replayed is False
    assert retried.attempt_count == 2


@pytest.mark.parametrize(
    ("mark_method", "expected_status"),
    [
        ("mark_failed", OperationStatus.FAILED_TERMINAL),
        ("mark_unknown", OperationStatus.UNKNOWN),
    ],
)
async def test_terminal_failure_and_unknown_outcome_replay_without_takeover(
    mark_method: str,
    expected_status: OperationStatus,
) -> None:
    state = MemoryState()
    clock = MutableClock()
    service = _service(state, clock)
    request = _request()
    await service.claim(request, lease_owner="worker-a")

    method = getattr(service, mark_method)
    if mark_method == "mark_failed":
        terminal = await method(
            request,
            lease_owner="worker-a",
            error_code="VALIDATION_FAILED",
            error_summary="invalid bounded request",
            retryable=False,
        )
    else:
        terminal = await method(
            request,
            lease_owner="worker-a",
            error_code="PROVIDER_OUTCOME_UNKNOWN",
            error_summary="provider disconnected after acceptance",
        )

    clock.advance(hours=1)
    replay = await service.claim(request, lease_owner="worker-b")
    assert terminal.status is expected_status
    assert replay.status is expected_status
    assert replay.replayed is True
    assert replay.attempt_count == 1


async def test_success_result_is_immutable_and_reused_by_later_claims() -> None:
    state = MemoryState()
    service = _service(state, MutableClock())
    request = _request()
    await service.claim(request, lease_owner="worker-a")
    success = await service.mark_succeeded(
        request,
        lease_owner="worker-a",
        result=EffectResult(
            resource_type="ticket",
            resource_id="INC-1001",
            resource_version=1,
            result_summary={"ticket_id": "INC-1001", "version": 1},
        ),
    )

    repeated_mark = await service.mark_succeeded(
        request,
        lease_owner="worker-b",
        result=EffectResult(
            resource_type="ticket",
            resource_id="INC-TAMPERED",
            resource_version=99,
            result_summary={"ticket_id": "INC-TAMPERED", "version": 99},
        ),
    )
    replay = await service.claim(request, lease_owner="worker-c")

    assert success.status is OperationStatus.SUCCEEDED
    assert success.resource_id == "INC-1001"
    assert repeated_mark.replayed is True
    assert repeated_mark.resource_id == "INC-1001"
    assert replay.replayed is True
    assert replay.resource_id == "INC-1001"
    assert replay.result_summary == {"ticket_id": "INC-1001", "version": 1}
    assert replay.attempt_count == 1


async def test_execute_once_commits_operation_and_ticket_result_in_the_same_uow() -> None:
    state = MemoryState()
    service = _service(state, MutableClock())

    result = await service.execute_once(
        _request(),
        lease_owner="worker-a",
        effect=_ticket_effect(state),
    )

    assert result.status is OperationStatus.SUCCEEDED
    assert result.replayed is False
    assert state.effect_calls == 1
    assert state.ticket_results["INC-1001"]["version"] == 1
    assert state.entries[str(OPERATION_ID)].resource_id == "INC-1001"
    assert state.entries[str(OPERATION_ID)].status is OperationStatus.SUCCEEDED


async def test_repeated_and_concurrent_execute_once_create_one_business_effect() -> None:
    state = MemoryState()
    service = _service(state, MutableClock())
    request = _request()
    effect = _ticket_effect(state)

    first, second = await asyncio.gather(
        service.execute_once(request, lease_owner="worker-a", effect=effect),
        service.execute_once(request, lease_owner="worker-b", effect=effect),
    )
    third = await service.execute_once(request, lease_owner="worker-c", effect=effect)

    assert state.effect_calls == 1
    assert list(state.ticket_results) == ["INC-1001"]
    assert {first.status, second.status, third.status} == {OperationStatus.SUCCEEDED}
    assert sum(result.replayed for result in (first, second, third)) == 2
    assert {result.resource_id for result in (first, second, third)} == {"INC-1001"}


@pytest.mark.parametrize(
    ("retryable", "expected_status"),
    [
        (True, OperationStatus.FAILED_RETRYABLE),
        (False, OperationStatus.FAILED_TERMINAL),
    ],
)
async def test_execute_once_rolls_back_effect_then_classifies_bounded_failure(
    retryable: bool,
    expected_status: OperationStatus,
) -> None:
    state = MemoryState()
    service = _service(state, MutableClock())

    async def failing_effect(uow: OperationUnitOfWork) -> EffectResult:
        assert isinstance(uow, MemoryUnitOfWork)
        state.effect_calls += 1
        uow.state.ticket_results["INC-ROLLED-BACK"] = {"id": "INC-ROLLED-BACK"}
        raise OperationEffectError(
            code="DEPENDENCY_TIMEOUT" if retryable else "INVALID_TOOL_RESULT",
            retryable=retryable,
            detail="api_key=super-secret-value " + ("x" * 2_000),
        )

    result = await service.execute_once(
        _request(),
        lease_owner="worker-a",
        effect=failing_effect,
    )

    assert result.status is expected_status
    assert "INC-ROLLED-BACK" not in state.ticket_results
    assert state.effect_calls == 1
    assert result.error_code in {"DEPENDENCY_TIMEOUT", "INVALID_TOOL_RESULT"}
    assert result.error_summary is not None
    assert "super-secret-value" not in result.error_summary
    assert len(result.error_summary) <= MAX_OPERATION_SUMMARY_CHARS


async def test_success_summary_is_deep_copied_bounded_and_redacted_before_replay() -> None:
    state = MemoryState()
    service = _service(state, MutableClock())
    source_summary = {
        "ticket": {
            "id": "INC-1001",
            "message": "Authorization: Bearer secret-token " + ("x" * 2_000),
        }
    }

    async def effect(uow: OperationUnitOfWork) -> EffectResult:
        del uow
        return EffectResult(
            resource_type="ticket",
            resource_id="INC-1001",
            resource_version=1,
            result_summary=source_summary,
        )

    success = await service.execute_once(
        _request(),
        lease_owner="worker-a",
        effect=effect,
    )
    source_summary["ticket"]["id"] = "INC-TAMPERED"
    replay = await service.claim(_request(), lease_owner="worker-b")

    assert success.status is OperationStatus.SUCCEEDED
    assert replay.result_summary is not source_summary
    assert replay.result_summary is not None
    assert replay.result_summary["ticket"]["id"] == "INC-1001"
    rendered = repr(replay.result_summary)
    assert "secret-token" not in rendered
    assert len(replay.result_summary["ticket"]["message"]) <= MAX_OPERATION_SUMMARY_CHARS
