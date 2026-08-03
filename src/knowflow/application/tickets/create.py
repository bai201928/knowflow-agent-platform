"""Idempotent ticket creation coordinated by the durable operation ledger.

The use case deliberately works through storage-neutral ports.  A single unit
of work owns the operation claim, ticket facts, audit trail, workflow
projection, and outbox record so a crash cannot expose a partial ticket.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, Self
from uuid import UUID, uuid4

from knowflow.application.auth.policy import AccessContext, Capability, require_capability
from knowflow.application.workflows.operations import (
    EffectResult,
    OperationLedgerService,
    OperationRequest,
    OperationUnitOfWork,
)
from knowflow.domain.common.errors import ErrorCode, KnowFlowError
from knowflow.domain.common.identity import payload_hash
from knowflow.infrastructure.db.models.ticketing import TicketSeverity, TicketStatus


@dataclass(frozen=True, slots=True)
class TicketCreateRequest:
    """Trusted command inputs plus explicitly non-authoritative model hints."""

    operation_id: str
    workflow_id: str
    title: str
    description: str
    severity: TicketSeverity
    model_actor_user_id: str | None = None
    model_assigned_team_id: str | None = None


@dataclass(frozen=True, slots=True)
class TicketCreateResult:
    ticket_id: str
    ticket_key: str
    ticket_version: int
    operation_id: str
    message_id: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class TicketAssignment:
    """Assignment selected by trusted server policy, never by model output."""

    team_id: str
    policy_version: int


@dataclass(frozen=True, slots=True)
class TicketFact:
    id: str
    key: str
    title: str
    description: str
    severity: TicketSeverity
    status: TicketStatus
    created_by_user_id: str
    assigned_team_id: str
    version: int
    sla_deadline: datetime
    sla_version: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TicketEventFact:
    id: str
    ticket_id: str
    ticket_version: int
    event_type: str
    actor_user_id: str
    operation_id: str
    before_summary: dict[str, Any] | None
    after_summary: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AuditEventFact:
    id: str
    occurred_at: datetime
    actor_user_id: str
    actor_session_id: str
    workflow_id: str
    ticket_id: str
    operation_id: str
    message_id: str
    action: str
    resource_type: str
    resource_id: str
    authorization_decision: str
    outcome: str
    redacted_metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class WorkflowTicketProjection:
    workflow_id: str
    ticket_id: str
    ticket_key: str
    ticket_version: int
    operation_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OutboxFact:
    id: str
    message_id: str
    event_type: str
    schema_version: int
    aggregate_type: str
    aggregate_id: str
    operation_id: str
    payload: dict[str, Any]
    trace_context: dict[str, Any]
    created_at: datetime


class TicketRepository(Protocol):
    async def add(self, ticket: TicketFact) -> None: ...


class TicketEventRepository(Protocol):
    async def append(self, event: TicketEventFact) -> None: ...


class AuditEventRepository(Protocol):
    async def append(self, event: AuditEventFact) -> None: ...


class WorkflowProjectionRepository(Protocol):
    async def record_ticket_created(self, projection: WorkflowTicketProjection) -> None: ...


class OutboxRepository(Protocol):
    async def add(self, event: OutboxFact) -> None: ...


class TicketCreateUnitOfWork(OperationUnitOfWork, Protocol):
    tickets: TicketRepository
    ticket_events: TicketEventRepository
    audit_events: AuditEventRepository
    workflow_projections: WorkflowProjectionRepository
    outbox: OutboxRepository

    async def __aenter__(self) -> Self: ...


class TicketAssignmentService(Protocol):
    async def resolve(
        self, context: AccessContext, *, severity: TicketSeverity
    ) -> TicketAssignment: ...


class SlaPolicy(Protocol):
    version: int

    def deadline_for(self, severity: TicketSeverity, *, created_at: datetime) -> datetime: ...


Clock = Callable[[], datetime]
UuidFactory = Callable[[], UUID]
TicketKeyFactory = Callable[[], str]


class TicketCreateService:
    """Create a ticket once and deterministically replay its durable result."""

    def __init__(
        self,
        *,
        operation_ledger: OperationLedgerService,
        assignment_service: TicketAssignmentService,
        sla_policy: SlaPolicy,
        clock: Clock,
        ticket_id_factory: UuidFactory = uuid4,
        ticket_key_factory: TicketKeyFactory,
        ticket_event_id_factory: UuidFactory = uuid4,
        audit_event_id_factory: UuidFactory = uuid4,
        outbox_id_factory: UuidFactory = uuid4,
        message_id_factory: UuidFactory = uuid4,
    ) -> None:
        self._operation_ledger = operation_ledger
        self._assignment_service = assignment_service
        self._sla_policy = sla_policy
        self._clock = clock
        self._ticket_id_factory = ticket_id_factory
        self._ticket_key_factory = ticket_key_factory
        self._ticket_event_id_factory = ticket_event_id_factory
        self._audit_event_id_factory = audit_event_id_factory
        self._outbox_id_factory = outbox_id_factory
        self._message_id_factory = message_id_factory

    async def create(
        self,
        request: TicketCreateRequest,
        *,
        context: AccessContext,
        lease_owner: str,
    ) -> TicketCreateResult:
        require_capability(context, Capability.TICKET_CREATE)
        self._validate_request(request)
        operation = OperationRequest(
            operation_id=request.operation_id,
            scope_type="workflow",
            scope_id=request.workflow_id,
            operation_type="ticket.create",
            payload_hash=payload_hash(
                {
                    "workflow_id": request.workflow_id,
                    "title": request.title,
                    "description": request.description,
                    "severity": request.severity.value,
                    "actor_user_id": context.user_id,
                    "actor_acl_scope": context.scope_fingerprint(),
                }
            ),
        )

        async def persist(uow: OperationUnitOfWork) -> EffectResult:
            ticket_uow = self._ticket_uow(uow)
            now = self._now()
            assignment = await self._assignment_service.resolve(
                context,
                severity=request.severity,
            )
            self._validate_assignment(assignment)
            deadline = self._sla_policy.deadline_for(request.severity, created_at=now)
            deadline = self._ensure_utc(deadline, field="SLA deadline")

            ticket_id = str(self._ticket_id_factory())
            ticket_key = self._ticket_key_factory().strip()
            if not ticket_key or len(ticket_key) > 32:
                raise ValueError("ticket key must be between 1 and 32 characters")
            ticket_version = 1
            message_id = str(self._message_id_factory())

            public_summary: dict[str, Any] = {
                "ticket_id": ticket_id,
                "ticket_key": ticket_key,
                "ticket_version": ticket_version,
                "severity": request.severity.value,
                "assigned_team_id": assignment.team_id,
                "assignment_policy_version": assignment.policy_version,
                "sla_deadline": deadline.isoformat().replace("+00:00", "Z"),
                "sla_version": self._sla_policy.version,
                "message_id": message_id,
            }
            event_payload: dict[str, Any] = {
                "ticket_id": ticket_id,
                "ticket_key": ticket_key,
                "ticket_version": ticket_version,
                "severity": request.severity.value,
                "assigned_team_id": assignment.team_id,
                "notification_policy": "ticket-created-v1",
            }

            await ticket_uow.tickets.add(
                TicketFact(
                    id=ticket_id,
                    key=ticket_key,
                    title=request.title,
                    description=request.description,
                    severity=request.severity,
                    status=TicketStatus.OPEN,
                    created_by_user_id=context.user_id,
                    assigned_team_id=assignment.team_id,
                    version=ticket_version,
                    sla_deadline=deadline,
                    sla_version=self._sla_policy.version,
                    created_at=now,
                )
            )
            await ticket_uow.ticket_events.append(
                TicketEventFact(
                    id=str(self._ticket_event_id_factory()),
                    ticket_id=ticket_id,
                    ticket_version=ticket_version,
                    event_type="ticket.created",
                    actor_user_id=context.user_id,
                    operation_id=request.operation_id,
                    before_summary=None,
                    after_summary=dict(public_summary),
                    created_at=now,
                )
            )
            await ticket_uow.audit_events.append(
                AuditEventFact(
                    id=str(self._audit_event_id_factory()),
                    occurred_at=now,
                    actor_user_id=context.user_id,
                    actor_session_id=context.session_id,
                    workflow_id=request.workflow_id,
                    ticket_id=ticket_id,
                    operation_id=request.operation_id,
                    message_id=message_id,
                    action="ticket.create",
                    resource_type="ticket",
                    resource_id=ticket_id,
                    authorization_decision="ALLOW",
                    outcome="SUCCEEDED",
                    redacted_metadata=dict(public_summary),
                )
            )
            await ticket_uow.workflow_projections.record_ticket_created(
                WorkflowTicketProjection(
                    workflow_id=request.workflow_id,
                    ticket_id=ticket_id,
                    ticket_key=ticket_key,
                    ticket_version=ticket_version,
                    operation_id=request.operation_id,
                    created_at=now,
                )
            )
            await ticket_uow.outbox.add(
                OutboxFact(
                    id=str(self._outbox_id_factory()),
                    message_id=message_id,
                    event_type="ticket.created",
                    schema_version=1,
                    aggregate_type="ticket",
                    aggregate_id=ticket_id,
                    operation_id=request.operation_id,
                    payload=event_payload,
                    trace_context={},
                    created_at=now,
                )
            )
            return EffectResult(
                resource_type="ticket",
                resource_id=ticket_id,
                resource_version=ticket_version,
                result_summary=public_summary,
            )

        entry = await self._operation_ledger.execute_once(
            operation,
            lease_owner=lease_owner,
            effect=persist,
        )
        summary = entry.result_summary
        if entry.resource_id is None or entry.resource_version is None or summary is None:
            raise KnowFlowError(
                ErrorCode.INTERNAL_ERROR,
                "Ticket operation did not produce a replayable result",
            )
        ticket_key = summary.get("ticket_key")
        message_id = summary.get("message_id")
        if not isinstance(ticket_key, str) or not isinstance(message_id, str):
            raise KnowFlowError(
                ErrorCode.INTERNAL_ERROR,
                "Ticket operation result is incomplete",
            )
        return TicketCreateResult(
            ticket_id=entry.resource_id,
            ticket_key=ticket_key,
            ticket_version=entry.resource_version,
            operation_id=entry.operation_id,
            message_id=message_id,
            replayed=entry.replayed,
        )

    @staticmethod
    def _ticket_uow(uow: OperationUnitOfWork) -> TicketCreateUnitOfWork:
        required = (
            "tickets",
            "ticket_events",
            "audit_events",
            "workflow_projections",
            "outbox",
        )
        if any(not hasattr(uow, name) for name in required):
            raise TypeError("operation unit of work does not provide ticket repositories")
        return uow  # type: ignore[return-value]

    def _now(self) -> datetime:
        return self._ensure_utc(self._clock(), field="ticket clock")

    @staticmethod
    def _ensure_utc(value: datetime, *, field: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _validate_assignment(assignment: TicketAssignment) -> None:
        if not assignment.team_id.strip():
            raise ValueError("server assignment must include a team")
        if assignment.policy_version < 1:
            raise ValueError("assignment policy version must be positive")

    @staticmethod
    def _validate_request(request: TicketCreateRequest) -> None:
        for field, value, maximum in (
            ("operation id", request.operation_id, 128),
            ("workflow id", request.workflow_id, 128),
            ("title", request.title, 200),
            ("description", request.description, 10_000),
        ):
            if not value.strip() or len(value) > maximum:
                raise ValueError(f"{field} must be between 1 and {maximum} characters")


__all__ = [
    "AuditEventRepository",
    "OutboxRepository",
    "SlaPolicy",
    "TicketAssignment",
    "TicketAssignmentService",
    "TicketCreateRequest",
    "TicketCreateResult",
    "TicketCreateService",
    "TicketCreateUnitOfWork",
    "TicketEventRepository",
    "TicketRepository",
    "WorkflowProjectionRepository",
]
