from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from knowflow.application.workflows.operations import (
    EffectResult,
    OperationLedgerService,
    OperationRequest,
)
from knowflow.domain.common.errors import ErrorCode, KnowFlowError
from knowflow.domain.common.identity import payload_hash


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    allowed_operations: frozenset[str]
    default_deadline_seconds: int = 60
    max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class SandboxResult:
    operation_id: str
    status: str
    resource_type: str | None = None
    resource_id: str | None = None
    resource_version: int | None = None
    result_summary: dict[str, Any] | None = None
    replayed: bool = False


Clock = Callable[[], datetime]


class SandboxExecutor:
    def __init__(
        self,
        *,
        config: SandboxConfig,
        operation_ledger: OperationLedgerService,
        clock: Clock,
    ) -> None:
        if not config.allowed_operations:
            raise ValueError("allowed_operations must not be empty")
        if config.default_deadline_seconds < 1:
            raise ValueError("default_deadline_seconds must be positive")
        if config.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._config = config
        self._ledger = operation_ledger
        self._clock = clock

    async def execute_operation(
        self,
        *,
        operation_id: str,
        operation_type: str,
        payload: dict[str, Any],
        deadline: datetime | None = None,
        lease_owner: str = "sandbox",
    ) -> SandboxResult:
        if operation_type not in self._config.allowed_operations:
            raise KnowFlowError(
                ErrorCode.PERMISSION_DENIED,
                f"Operation {operation_type!r} is not in the sandbox allowlist",
                retryable=False,
            )
        self._validate_operation_id(operation_id)

        effective_dt = deadline or (
            self._now() + timedelta(seconds=self._config.default_deadline_seconds)
        )
        now = self._now()
        if effective_dt <= now:
            raise KnowFlowError(
                ErrorCode.DEADLINE_EXCEEDED,
                "Sandbox operation deadline already passed",
            )

        payload_bytes = payload_hash(payload)
        operation = OperationRequest(
            operation_id=operation_id,
            scope_type="sandbox",
            scope_id="default",
            operation_type=operation_type,
            payload_hash=payload_bytes,
        )

        async def execute(uow: Any) -> EffectResult:
            _ = uow
            return EffectResult(
                resource_type="sandbox_operation",
                resource_id=operation_id,
                resource_version=1,
                result_summary={
                    "operation_type": operation_type,
                    "executed": "stub",
                    "timestamp": self._now().isoformat().replace("+00:00", "Z"),
                },
            )

        entry = await self._ledger.execute_once(
            operation, lease_owner=lease_owner, effect=execute
        )
        summary = entry.result_summary or {}
        return SandboxResult(
            operation_id=entry.operation_id,
            status=entry.status.value,
            resource_type=entry.resource_type,
            resource_id=entry.resource_id,
            resource_version=entry.resource_version,
            result_summary=summary,
            replayed=entry.replayed,
        )

    async def lookup_operation_status(
        self, operation_id: str
    ) -> dict[str, Any]:
        self._validate_operation_id(operation_id)
        operation = OperationRequest(
            operation_id=operation_id,
            scope_type="sandbox",
            scope_id="default",
            operation_type="__lookup__",
            payload_hash="0" * 64,
        )
        entry = await self._ledger.claim(operation, lease_owner="sandbox-lookup")
        return {
            "operation_id": entry.operation_id,
            "status": entry.status.value,
            "result_summary": entry.result_summary,
            "attempt_count": entry.attempt_count,
        }

    @staticmethod
    def _validate_operation_id(operation_id: str) -> None:
        if not operation_id or not operation_id.strip() or len(operation_id) > 128:
            raise ValueError(
                "operation_id must be between 1 and 128 characters"
            )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("sandbox clock must return timezone-aware datetime")
        return now.astimezone(UTC)


__all__ = [
    "SandboxConfig",
    "SandboxExecutor",
    "SandboxResult",
]
