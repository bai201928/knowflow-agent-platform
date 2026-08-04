from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, Self

from knowflow.application.auth.policy import (
    AccessContext,
    Capability,
    require_capability,
)
from knowflow.application.workflows.operations import (
    EffectResult,
    OperationLedgerService,
    OperationRequest,
    OperationUnitOfWork,
)
from knowflow.domain.common.errors import concealed_not_found
from knowflow.domain.common.identity import payload_hash
from knowflow.infrastructure.db.models.ticketing import Approval, ApprovalStatus


@dataclass(frozen=True, slots=True)
class ApprovalCreateRequest:
    operation_id: str
    workflow_id: str
    plan_id: str
    plan_version: int
    task_id: str
    action_type: str
    resource_type: str
    resource_id: str
    resource_version: int | None = None
    normalized_parameters: dict[str, Any] | None = None
    requester_user_id: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ApprovalDecisionRequest:
    approval_id: str
    decision: str
    reason: str
    approver_user_id: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    approval_id: str
    workflow_id: str
    status: str
    decided_at: datetime | None = None
    decision_reason: str | None = None
    replayed: bool = False


class ApprovalRepository(Protocol):
    async def get_for_update(
        self, approval_id: str
    ) -> Approval | None: ...
    async def add(self, approval: Approval) -> None: ...
    async def save(self, approval: Approval) -> None: ...


class WorkflowCommandRepository(Protocol):
    async def add_command(
        self,
        *,
        workflow_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> str: ...


class ApprovalUnitOfWork(OperationUnitOfWork, Protocol):
    approvals: ApprovalRepository
    workflow_commands: WorkflowCommandRepository
    async def __aenter__(self) -> Self: ...


ApprovalUnitOfWorkFactory = Callable[[], ApprovalUnitOfWork]
Clock = Callable[[], datetime]

_DECISION_ALLOWED = frozenset({"APPROVED", "REJECTED"})
_TERMINAL_STATUSES = frozenset({
    ApprovalStatus.APPROVED,
    ApprovalStatus.REJECTED,
    ApprovalStatus.EXPIRED,
    ApprovalStatus.INVALIDATED,
})


class ApprovalService:
    def __init__(
        self,
        *,
        operation_ledger: OperationLedgerService,
        uow_factory: ApprovalUnitOfWorkFactory,
        clock: Clock,
    ) -> None:
        self._ledger = operation_ledger
        self._uow_factory = uow_factory
        self._clock = clock

    async def create_approval(
        self,
        request: ApprovalCreateRequest,
        *,
        context: AccessContext,
        lease_owner: str,
    ) -> ApprovalResult:
        self._validate_create_request(request)
        requester = request.requester_user_id or context.user_id
        operation = OperationRequest(
            operation_id=request.operation_id,
            scope_type="workflow",
            scope_id=request.workflow_id,
            operation_type="approval.create",
            payload_hash=payload_hash({
                "workflow_id": request.workflow_id,
                "plan_id": request.plan_id,
                "plan_version": request.plan_version,
                "task_id": request.task_id,
                "action_type": request.action_type,
                "resource_type": request.resource_type,
                "resource_id": request.resource_id,
                "resource_version": request.resource_version,
            }),
        )

        async def persist(uow: OperationUnitOfWork) -> EffectResult:
            auow = self._cast_uow(uow)
            now = self._now()
            approval = Approval(
                workflow_id=request.workflow_id,
                plan_id=request.plan_id,
                plan_version=request.plan_version,
                task_id=request.task_id,
                operation_id=request.operation_id,
                action_type=request.action_type,
                resource_type=request.resource_type,
                resource_id=request.resource_id,
                resource_version=request.resource_version,
                normalized_parameters=request.normalized_parameters or {},
                payload_hash=operation.payload_hash,
                requester_user_id=requester,
                status=ApprovalStatus.PENDING,
                expires_at=request.expires_at or (now + timedelta(hours=24)),
                version=1,
                created_at=now,
            )
            await auow.approvals.add(approval)
            return EffectResult(
                resource_type="approval",
                resource_id=approval.id,
                resource_version=1,
                result_summary={
                    "approval_id": approval.id,
                    "workflow_id": request.workflow_id,
                    "status": ApprovalStatus.PENDING.value,
                },
            )

        entry = await self._ledger.execute_once(
            operation, lease_owner=lease_owner, effect=persist
        )
        summary = entry.result_summary or {}
        return ApprovalResult(
            approval_id=str(
                summary.get("approval_id", entry.resource_id or "")
            ),
            workflow_id=str(summary.get("workflow_id", "")),
            status=str(summary.get("status", "")),
            replayed=entry.replayed,
        )

    async def decide_approval(
        self,
        request: ApprovalDecisionRequest,
        *,
        context: AccessContext,
    ) -> ApprovalResult:
        require_capability(context, Capability.APPROVAL_DECIDE)
        self._validate_decision_request(request)
        async with self._uow_factory() as uow:
            approval = await uow.approvals.get_for_update(
                request.approval_id
            )
            if approval is None:
                raise concealed_not_found("Approval not found")
            if approval.status in _TERMINAL_STATUSES:
                return ApprovalResult(
                    approval_id=approval.id,
                    workflow_id=approval.workflow_id,
                    status=approval.status.value,
                    decided_at=approval.decided_at,
                    decision_reason=approval.decision_reason,
                    replayed=True,
                )
            now = self._now()
            if approval.expires_at <= now:
                approval.status = ApprovalStatus.EXPIRED
                approval.version += 1
                approval.updated_at = now
                await uow.approvals.save(approval)
                await uow.commit()
                return ApprovalResult(
                    approval_id=approval.id,
                    workflow_id=approval.workflow_id,
                    status=ApprovalStatus.EXPIRED.value,
                    replayed=False,
                )
            decision_status = ApprovalStatus(request.decision)
            approval.status = decision_status
            approval.approver_user_id = (
                request.approver_user_id or context.user_id
            )
            approval.decision_reason = request.reason
            approval.decided_at = now
            approval.version += 1
            approval.updated_at = now
            await uow.approvals.save(approval)
            if decision_status is ApprovalStatus.APPROVED:
                await uow.workflow_commands.add_command(
                    workflow_id=approval.workflow_id,
                    kind="APPROVAL_RESUME",
                    payload={
                        "approval_id": approval.id,
                        "plan_id": approval.plan_id,
                        "plan_version": approval.plan_version,
                        "task_id": approval.task_id,
                        "decision": decision_status.value,
                    },
                )
            await uow.commit()
            return ApprovalResult(
                approval_id=approval.id,
                workflow_id=approval.workflow_id,
                status=decision_status.value,
                decided_at=now,
                decision_reason=request.reason,
                replayed=False,
            )

    async def invalidate_approval(
        self,
        approval_id: str,
        *,
        reason: str,
        context: AccessContext,
    ) -> ApprovalResult:
        _ = context
        async with self._uow_factory() as uow:
            approval = await uow.approvals.get_for_update(approval_id)
            if approval is None:
                raise concealed_not_found("Approval not found")
            if approval.status is not ApprovalStatus.PENDING:
                return ApprovalResult(
                    approval_id=approval.id,
                    workflow_id=approval.workflow_id,
                    status=approval.status.value,
                    decided_at=approval.decided_at,
                    decision_reason=approval.decision_reason,
                    replayed=True,
                )
            now = self._now()
            approval.status = ApprovalStatus.INVALIDATED
            approval.decision_reason = reason
            approval.decided_at = now
            approval.version += 1
            approval.updated_at = now
            await uow.approvals.save(approval)
            await uow.commit()
            return ApprovalResult(
                approval_id=approval.id,
                workflow_id=approval.workflow_id,
                status=ApprovalStatus.INVALIDATED.value,
                decided_at=now,
                decision_reason=reason,
                replayed=False,
            )

    @staticmethod
    def _cast_uow(uow: OperationUnitOfWork) -> ApprovalUnitOfWork:
        required = ("approvals", "workflow_commands")
        if any(not hasattr(uow, name) for name in required):
            raise TypeError(
                "unit of work does not provide approval repositories"
            )
        return uow  # type: ignore[return-value]

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError(
                "approval clock must return timezone-aware datetime"
            )
        return now.astimezone(UTC)

    @staticmethod
    def _validate_create_request(
        request: ApprovalCreateRequest,
    ) -> None:
        for f_name, f_value, f_max in (
            ("operation_id", request.operation_id, 128),
            ("workflow_id", request.workflow_id, 128),
            ("plan_id", request.plan_id, 128),
            ("task_id", request.task_id, 128),
            ("action_type", request.action_type, 128),
            ("resource_type", request.resource_type, 64),
            ("resource_id", request.resource_id, 128),
        ):
            if not f_value.strip() or len(f_value) > f_max:
                raise ValueError(
                    f"{f_name} must be between 1 and {f_max} characters"
                )
        if request.plan_version < 1:
            raise ValueError("plan_version must be positive")

    @staticmethod
    def _validate_decision_request(
        request: ApprovalDecisionRequest,
    ) -> None:
        if not request.approval_id.strip():
            raise ValueError("approval_id is required")
        if request.decision not in _DECISION_ALLOWED:
            raise ValueError(
                f"decision must be one of {sorted(_DECISION_ALLOWED)}"
            )
        if not request.reason.strip() or len(request.reason) > 1000:
            raise ValueError(
                "reason must be between 1 and 1000 characters"
            )


__all__ = [
    "ApprovalCreateRequest",
    "ApprovalDecisionRequest",
    "ApprovalRepository",
    "ApprovalResult",
    "ApprovalService",
    "ApprovalUnitOfWork",
    "ApprovalUnitOfWorkFactory",
    "WorkflowCommandRepository",
]
