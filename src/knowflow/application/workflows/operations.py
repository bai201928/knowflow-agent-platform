"""Durable operation-ledger coordination for repeat-safe business effects.

The service owns operation identity, leases, retries, and immutable terminal
results.  Repositories provide the database-specific row lock while the unit of
work keeps a successful business effect and its ledger result in one commit.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, Self

from knowflow.domain.common.errors import ErrorCode, KnowFlowError
from knowflow.infrastructure.db.models.workflow import OperationStatus

MAX_OPERATION_SUMMARY_CHARS: Final = 500
"""Maximum persisted length for any free-form operation summary string."""

_TERMINAL_STATUSES: Final = frozenset(
    {
        OperationStatus.SUCCEEDED,
        OperationStatus.FAILED_TERMINAL,
        OperationStatus.UNKNOWN,
    }
)
_MAX_SUMMARY_DEPTH: Final = 8
_MAX_SUMMARY_ITEMS: Final = 50
_SENSITIVE_KEY: Final = re.compile(
    r"(?:api[-_]?key|authorization|cookie|credential|password|secret|token)",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_PATTERNS: Final = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"),
    re.compile(
        r"(?i)((?:api[-_]?key|password|secret|token)\s*[:=]\s*)"
        r"(?:['\"]?)[^\s,;'\"]+(?:['\"]?)"
    ),
)


@dataclass(frozen=True, slots=True)
class OperationRequest:
    """Server-derived identity and payload fingerprint for one operation."""

    operation_id: str
    scope_type: str
    scope_id: str
    operation_type: str
    payload_hash: str


@dataclass(frozen=True, slots=True)
class EffectResult:
    """Bounded business result recorded for deterministic replay."""

    resource_type: str
    resource_id: str
    resource_version: int | None = None
    result_summary: Mapping[str, Any] | None = None


@dataclass(slots=True)
class LedgerEntry:
    """Storage-neutral operation row and the replay view returned to callers."""

    operation_id: str
    scope_type: str
    scope_id: str
    operation_type: str
    payload_hash: str
    status: OperationStatus
    attempt_count: int
    lease_owner: str | None
    lease_until: datetime | None
    heartbeat_at: datetime | None
    created_at: datetime
    updated_at: datetime
    resource_type: str | None = None
    resource_id: str | None = None
    resource_version: int | None = None
    result_summary: dict[str, Any] | None = None
    error_code: str | None = None
    error_summary: str | None = None
    replayed: bool = False

    @property
    def last_error_code(self) -> str | None:
        """Alias matching the persistence model's column name."""

        return self.error_code

    @property
    def last_error_summary(self) -> str | None:
        """Alias matching the persistence model's column name."""

        return self.error_summary


class OperationRepository(Protocol):
    """Repository operations required while holding an operation row lock."""

    async def get_for_update(self, operation_id: str) -> LedgerEntry | None: ...

    async def add(self, entry: LedgerEntry) -> None: ...

    async def save(self, entry: LedgerEntry) -> None: ...


class OperationUnitOfWork(Protocol):
    """Transaction boundary shared by the ledger and the business effect."""

    operations: OperationRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


OperationEffect = Callable[[OperationUnitOfWork], Awaitable[EffectResult]]
OperationUnitOfWorkFactory = Callable[[], OperationUnitOfWork]
Clock = Callable[[], datetime]


class OperationEffectError(Exception):
    """Expected, classified effect failure whose detail must not be persisted raw."""

    def __init__(self, *, code: str, retryable: bool, detail: str) -> None:
        super().__init__("Operation effect failed")
        self.code = _bounded_label(code, field="error code", maximum=64)
        self.retryable = retryable
        self.detail = detail


class OperationLedgerService:
    """Coordinate exclusive operation attempts and replay immutable outcomes."""

    def __init__(
        self,
        *,
        uow_factory: OperationUnitOfWorkFactory,
        clock: Clock,
        lease_duration: timedelta = timedelta(seconds=30),
    ) -> None:
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        self._uow_factory = uow_factory
        self._clock = clock
        self._lease_duration = lease_duration

    async def claim(self, request: OperationRequest, *, lease_owner: str) -> LedgerEntry:
        """Claim a new/retryable operation or replay the currently known outcome."""

        self._validate_request(request)
        owner = _bounded_label(lease_owner, field="lease owner", maximum=128)
        async with self._uow_factory() as uow:
            entry = await self._claim_locked(
                uow.operations,
                request=request,
                lease_owner=owner,
                now=self._now(),
            )
            await uow.commit()
            return entry

    async def mark_succeeded(
        self,
        request: OperationRequest,
        *,
        lease_owner: str,
        result: EffectResult,
    ) -> LedgerEntry:
        """Persist an immutable successful result owned by the active lease."""

        self._validate_request(request)
        owner = _bounded_label(lease_owner, field="lease owner", maximum=128)
        async with self._uow_factory() as uow:
            entry = await self._load_for_transition(
                uow.operations,
                request=request,
                lease_owner=owner,
                now=self._now(),
            )
            if entry.status in _TERMINAL_STATUSES:
                await uow.commit()
                return _view(entry, replayed=True)
            self._apply_success(entry, result=result, now=self._now())
            await uow.operations.save(entry)
            await uow.commit()
            return _view(entry, replayed=False)

    async def mark_failed(
        self,
        request: OperationRequest,
        *,
        lease_owner: str,
        error_code: str,
        error_summary: str,
        retryable: bool,
    ) -> LedgerEntry:
        """Record a known failure and whether another claimed attempt is safe."""

        status = OperationStatus.FAILED_RETRYABLE if retryable else OperationStatus.FAILED_TERMINAL
        return await self._mark_error(
            request,
            lease_owner=lease_owner,
            status=status,
            error_code=error_code,
            error_summary=error_summary,
        )

    async def mark_unknown(
        self,
        request: OperationRequest,
        *,
        lease_owner: str,
        error_code: str,
        error_summary: str,
    ) -> LedgerEntry:
        """Freeze an ambiguous external outcome for explicit reconciliation."""

        return await self._mark_error(
            request,
            lease_owner=lease_owner,
            status=OperationStatus.UNKNOWN,
            error_code=error_code,
            error_summary=error_summary,
        )

    async def execute_once(
        self,
        request: OperationRequest,
        *,
        lease_owner: str,
        effect: OperationEffect,
    ) -> LedgerEntry:
        """Execute and commit one business effect with its successful ledger result.

        A classified failure first rolls back the effect transaction, then records a
        sanitized failure result in a fresh transaction on the same unit-of-work
        abstraction. Unexpected programming failures are allowed to propagate so the
        context manager can roll the entire transaction back.
        """

        self._validate_request(request)
        owner = _bounded_label(lease_owner, field="lease owner", maximum=128)
        async with self._uow_factory() as uow:
            claimed = await self._claim_locked(
                uow.operations,
                request=request,
                lease_owner=owner,
                now=self._now(),
            )
            if claimed.replayed:
                await uow.commit()
                return claimed

            try:
                effect_result = await effect(uow)
                if not isinstance(effect_result, EffectResult):
                    raise OperationEffectError(
                        code="INVALID_TOOL_RESULT",
                        retryable=False,
                        detail="Effect returned an invalid result type",
                    )
            except OperationEffectError as error:
                await uow.rollback()
                failed = await self._restore_classified_failure(
                    uow.operations,
                    request=request,
                    claimed=claimed,
                    error=error,
                    now=self._now(),
                )
                await uow.commit()
                return _view(failed, replayed=False)

            entry = await uow.operations.get_for_update(request.operation_id)
            if entry is None:
                raise KnowFlowError(
                    ErrorCode.INTERNAL_ERROR,
                    "Claimed operation disappeared before commit",
                )
            self._assert_same_identity(entry, request)
            self._assert_active_owner(entry, lease_owner=owner, now=self._now())
            self._apply_success(entry, result=effect_result, now=self._now())
            await uow.operations.save(entry)
            await uow.commit()
            return _view(entry, replayed=False)

    async def _mark_error(
        self,
        request: OperationRequest,
        *,
        lease_owner: str,
        status: OperationStatus,
        error_code: str,
        error_summary: str,
    ) -> LedgerEntry:
        self._validate_request(request)
        owner = _bounded_label(lease_owner, field="lease owner", maximum=128)
        code = _bounded_label(error_code, field="error code", maximum=64)
        async with self._uow_factory() as uow:
            now = self._now()
            entry = await uow.operations.get_for_update(request.operation_id)
            if entry is None:
                raise KnowFlowError(ErrorCode.RESOURCE_NOT_FOUND, "Operation was not claimed")
            self._assert_same_identity(entry, request)
            if (
                entry.status in _TERMINAL_STATUSES
                or entry.status is OperationStatus.FAILED_RETRYABLE
            ):
                await uow.commit()
                return _view(entry, replayed=True)
            self._assert_active_owner(entry, lease_owner=owner, now=now)
            self._apply_error(
                entry,
                status=status,
                error_code=code,
                error_summary=error_summary,
                now=now,
            )
            await uow.operations.save(entry)
            await uow.commit()
            return _view(entry, replayed=False)

    async def _claim_locked(
        self,
        repository: OperationRepository,
        *,
        request: OperationRequest,
        lease_owner: str,
        now: datetime,
    ) -> LedgerEntry:
        entry = await repository.get_for_update(request.operation_id)
        if entry is None:
            entry = LedgerEntry(
                operation_id=request.operation_id,
                scope_type=request.scope_type,
                scope_id=request.scope_id,
                operation_type=request.operation_type,
                payload_hash=request.payload_hash,
                status=OperationStatus.RUNNING,
                attempt_count=1,
                lease_owner=lease_owner,
                lease_until=now + self._lease_duration,
                heartbeat_at=now,
                created_at=now,
                updated_at=now,
            )
            await repository.add(entry)
            return _view(entry, replayed=False)

        self._assert_same_identity(entry, request)
        if entry.status in _TERMINAL_STATUSES:
            return _view(entry, replayed=True)
        if (
            entry.status is OperationStatus.RUNNING
            and entry.lease_until is not None
            and entry.lease_until > now
        ):
            return _view(entry, replayed=True)

        entry.status = OperationStatus.RUNNING
        entry.attempt_count += 1
        entry.lease_owner = lease_owner
        entry.lease_until = now + self._lease_duration
        entry.heartbeat_at = now
        entry.error_code = None
        entry.error_summary = None
        entry.updated_at = now
        await repository.save(entry)
        return _view(entry, replayed=False)

    async def _load_for_transition(
        self,
        repository: OperationRepository,
        *,
        request: OperationRequest,
        lease_owner: str,
        now: datetime,
    ) -> LedgerEntry:
        entry = await repository.get_for_update(request.operation_id)
        if entry is None:
            raise KnowFlowError(ErrorCode.RESOURCE_NOT_FOUND, "Operation was not claimed")
        self._assert_same_identity(entry, request)
        if entry.status not in _TERMINAL_STATUSES:
            self._assert_active_owner(entry, lease_owner=lease_owner, now=now)
        return entry

    async def _restore_classified_failure(
        self,
        repository: OperationRepository,
        *,
        request: OperationRequest,
        claimed: LedgerEntry,
        error: OperationEffectError,
        now: datetime,
    ) -> LedgerEntry:
        entry = await repository.get_for_update(request.operation_id)
        if entry is None:
            entry = replace(claimed)
            await repository.add(entry)
        else:
            self._assert_same_identity(entry, request)
            entry.attempt_count = claimed.attempt_count

        self._apply_error(
            entry,
            status=(
                OperationStatus.FAILED_RETRYABLE
                if error.retryable
                else OperationStatus.FAILED_TERMINAL
            ),
            error_code=error.code,
            error_summary=error.detail,
            now=now,
        )
        await repository.save(entry)
        return entry

    @staticmethod
    def _apply_success(entry: LedgerEntry, *, result: EffectResult, now: datetime) -> None:
        entry.status = OperationStatus.SUCCEEDED
        entry.resource_type = _bounded_label(
            result.resource_type,
            field="resource type",
            maximum=64,
        )
        entry.resource_id = _bounded_label(
            result.resource_id,
            field="resource id",
            maximum=128,
        )
        entry.resource_version = result.resource_version
        entry.result_summary = _sanitize_summary(result.result_summary)
        entry.error_code = None
        entry.error_summary = None
        entry.lease_owner = None
        entry.lease_until = None
        entry.heartbeat_at = now
        entry.updated_at = now

    @staticmethod
    def _apply_error(
        entry: LedgerEntry,
        *,
        status: OperationStatus,
        error_code: str,
        error_summary: str,
        now: datetime,
    ) -> None:
        entry.status = status
        entry.error_code = error_code
        entry.error_summary = _sanitize_text(error_summary)
        entry.lease_owner = None
        entry.lease_until = None
        entry.heartbeat_at = now
        entry.updated_at = now

    @staticmethod
    def _assert_same_identity(entry: LedgerEntry, request: OperationRequest) -> None:
        same_identity = (
            entry.scope_type == request.scope_type
            and entry.scope_id == request.scope_id
            and entry.operation_type == request.operation_type
        )
        if entry.payload_hash != request.payload_hash or not same_identity:
            raise KnowFlowError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "Operation identity is already bound to a different request",
                retryable=False,
            )

    @staticmethod
    def _assert_active_owner(
        entry: LedgerEntry,
        *,
        lease_owner: str,
        now: datetime,
    ) -> None:
        if entry.status is not OperationStatus.RUNNING:
            raise KnowFlowError(
                ErrorCode.VERSION_CONFLICT,
                "Operation is not in a mutable running state",
                retryable=False,
            )
        if (
            entry.lease_owner != lease_owner
            or entry.lease_until is None
            or entry.lease_until <= now
        ):
            raise KnowFlowError(
                ErrorCode.VERSION_CONFLICT,
                "Operation lease is not owned by this worker",
                retryable=True,
            )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("operation clock must return a timezone-aware datetime")
        return now.astimezone(UTC)

    @staticmethod
    def _validate_request(request: OperationRequest) -> None:
        _bounded_label(request.operation_id, field="operation id", maximum=128)
        _bounded_label(request.scope_type, field="scope type", maximum=64)
        _bounded_label(request.scope_id, field="scope id", maximum=128)
        _bounded_label(request.operation_type, field="operation type", maximum=128)
        if len(request.payload_hash) != 64 or not all(
            character in "0123456789abcdefABCDEF" for character in request.payload_hash
        ):
            raise ValueError("payload_hash must be a 64-character hexadecimal digest")


def _view(entry: LedgerEntry, *, replayed: bool) -> LedgerEntry:
    copied = deepcopy(entry)
    copied.replayed = replayed
    return copied


def _bounded_label(value: str, *, field: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field} must be between 1 and {maximum} characters")
    return normalized


def _sanitize_text(value: str) -> str:
    sanitized = value
    for pattern in _SENSITIVE_VALUE_PATTERNS:
        sanitized = pattern.sub(r"\1[REDACTED]", sanitized)
    return sanitized[:MAX_OPERATION_SUMMARY_CHARS]


def _sanitize_summary(
    value: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if value is None:
        return None
    sanitized = _sanitize_value(value, depth=0)
    if not isinstance(sanitized, dict):
        raise TypeError("operation result summary must be a mapping")
    return sanitized


def _sanitize_value(value: Any, *, depth: int) -> Any:
    if depth >= _MAX_SUMMARY_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return _sanitize_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return deepcopy(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= _MAX_SUMMARY_ITEMS:
                result["__truncated__"] = True
                break
            key = _sanitize_text(str(raw_key))
            result[key] = (
                "[REDACTED]"
                if _SENSITIVE_KEY.search(key)
                else _sanitize_value(item, depth=depth + 1)
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_sanitize_value(item, depth=depth + 1) for item in value[:_MAX_SUMMARY_ITEMS]]
    return _sanitize_text(str(value))


__all__ = [
    "MAX_OPERATION_SUMMARY_CHARS",
    "EffectResult",
    "LedgerEntry",
    "OperationEffect",
    "OperationEffectError",
    "OperationLedgerService",
    "OperationRepository",
    "OperationRequest",
    "OperationUnitOfWork",
    "OperationUnitOfWorkFactory",
]
