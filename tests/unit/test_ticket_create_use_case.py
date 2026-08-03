"""T037 red tests for the idempotent P1 ticket-create application use case.

The fakes model one transaction shared with ``OperationLedgerService``.  The
tests intentionally avoid SQLAlchemy so failures identify application-boundary
mistakes instead of database setup problems.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from knowflow.application.tickets.create import (
    AuditEventRepository,
    OutboxRepository,
    SlaPolicy,
    TicketAssignment,
    TicketAssignmentService,
    TicketCreateRequest,
    TicketCreateResult,
    TicketCreateService,
    TicketCreateUnitOfWork,
    TicketEventRepository,
    TicketRepository,
    WorkflowProjectionRepository,
)

from knowflow.application.auth.policy import AccessContext
from knowflow.application.workflows.operations import (
    LedgerEntry,
    OperationLedgerService,
    OperationRepository,
    OperationUnitOfWork,
)
from knowflow.domain.common.errors import ErrorCode, KnowFlowError
from knowflow.infrastructure.db.models.identity import RoleCode
from knowflow.infrastructure.db.models.ticketing import TicketSeverity

NOW = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
WORKFLOW_ID = UUID("11111111-1111-4111-8111-111111111111")
OPERATION_ID = UUID("22222222-2222-4222-8222-222222222222")
TICKET_ID = UUID("33333333-3333-4333-8333-333333333333")
TICKET_EVENT_ID = UUID("44444444-4444-4444-8444-444444444444")
AUDIT_EVENT_ID = UUID("55555555-5555-4555-8555-555555555555")
OUTBOX_ID = UUID("66666666-6666-4666-8666-666666666666")
MESSAGE_ID = UUID("77777777-7777-4777-8777-777777777777")
ACTOR_ID = "88888888-8888-4888-8888-888888888888"
ACTOR_SESSION_ID = "99999999-9999-4999-8999-999999999999"
TRUSTED_TEAM_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class MemoryState:
    def __init__(self) -> None:
        self.operations: dict[str, LedgerEntry] = {}
        self.tickets: list[Any] = []
        self.ticket_events: list[Any] = []
        self.audit_events: list[Any] = []
        self.workflow_projections: list[Any] = []
        self.outbox_events: list[Any] = []
        self.lock = asyncio.Lock()
        self.commits = 0
        self.rollbacks = 0
        self.fail_outbox = False

    def business_counts(self) -> tuple[int, int, int, int, int]:
        return (
            len(self.tickets),
            len(self.ticket_events),
            len(self.audit_events),
            len(self.workflow_projections),
            len(self.outbox_events),
        )


class MemoryOperationRepository:
    def __init__(self, state: MemoryState) -> None:
        self._state = state

    async def get_for_update(self, operation_id: str) -> LedgerEntry | None:
        return self._state.operations.get(operation_id)

    async def add(self, entry: LedgerEntry) -> None:
        if entry.operation_id in self._state.operations:
            raise RuntimeError("duplicate operation")
        self._state.operations[entry.operation_id] = entry

    async def save(self, entry: LedgerEntry) -> None:
        self._state.operations[entry.operation_id] = entry


class MemoryTicketRepository:
    def __init__(self, state: MemoryState) -> None:
        self._state = state

    async def add(self, ticket: Any) -> None:
        self._state.tickets.append(ticket)


class MemoryTicketEventRepository:
    def __init__(self, state: MemoryState) -> None:
        self._state = state

    async def append(self, event: Any) -> None:
        self._state.ticket_events.append(event)


class MemoryAuditEventRepository:
    def __init__(self, state: MemoryState) -> None:
        self._state = state

    async def append(self, event: Any) -> None:
        self._state.audit_events.append(event)


class MemoryWorkflowProjectionRepository:
    def __init__(self, state: MemoryState) -> None:
        self._state = state

    async def record_ticket_created(self, projection: Any) -> None:
        self._state.workflow_projections.append(projection)


class MemoryOutboxRepository:
    def __init__(self, state: MemoryState) -> None:
        self._state = state

    async def add(self, event: Any) -> None:
        if self._state.fail_outbox:
            raise RuntimeError("injected outbox write failure")
        self._state.outbox_events.append(event)


class MemoryUnitOfWork:
    """Serializable rollback-capable fake spanning ledger and all business writes."""

    def __init__(self, state: MemoryState) -> None:
        self._state = state
        self.operations: OperationRepository = MemoryOperationRepository(state)
        self.tickets: TicketRepository = MemoryTicketRepository(state)
        self.ticket_events: TicketEventRepository = MemoryTicketEventRepository(state)
        self.audit_events: AuditEventRepository = MemoryAuditEventRepository(state)
        self.workflow_projections: WorkflowProjectionRepository = (
            MemoryWorkflowProjectionRepository(state)
        )
        self.outbox: OutboxRepository = MemoryOutboxRepository(state)
        self._before: dict[str, Any] = {}
        self._committed = False

    async def __aenter__(self) -> MemoryUnitOfWork:
        await self._state.lock.acquire()
        self._before = {
            "operations": deepcopy(self._state.operations),
            "tickets": deepcopy(self._state.tickets),
            "ticket_events": deepcopy(self._state.ticket_events),
            "audit_events": deepcopy(self._state.audit_events),
            "workflow_projections": deepcopy(self._state.workflow_projections),
            "outbox_events": deepcopy(self._state.outbox_events),
        }
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc, traceback
        if exc_type is not None or not self._committed:
            self._restore()
        self._state.lock.release()

    async def commit(self) -> None:
        self._committed = True
        self._state.commits += 1

    async def rollback(self) -> None:
        self._restore()
        self._committed = False
        self._state.rollbacks += 1

    def _restore(self) -> None:
        self._state.operations = deepcopy(self._before["operations"])
        self._state.tickets = deepcopy(self._before["tickets"])
        self._state.ticket_events = deepcopy(self._before["ticket_events"])
        self._state.audit_events = deepcopy(self._before["audit_events"])
        self._state.workflow_projections = deepcopy(self._before["workflow_projections"])
        self._state.outbox_events = deepcopy(self._before["outbox_events"])


class MemoryUowFactory:
    def __init__(self, state: MemoryState) -> None:
        self._state = state

    def __call__(self) -> TicketCreateUnitOfWork:
        return MemoryUnitOfWork(self._state)


class TrustedAssignmentService:
    def __init__(self) -> None:
        self.contexts: list[AccessContext] = []

    async def resolve(
        self, context: AccessContext, *, severity: TicketSeverity
    ) -> TicketAssignment:
        self.contexts.append(context)
        assert severity is TicketSeverity.P1
        return TicketAssignment(team_id=TRUSTED_TEAM_ID, policy_version=3)


class FixedSlaPolicy:
    version = 7

    def deadline_for(self, severity: TicketSeverity, *, created_at: datetime) -> datetime:
        assert severity is TicketSeverity.P1
        return created_at + timedelta(minutes=15)


def _context() -> AccessContext:
    return AccessContext(
        user_id=ACTOR_ID,
        session_id=ACTOR_SESSION_ID,
        roles=frozenset({RoleCode.EMPLOYEE}),
        team_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        acl_version=4,
    )


def _request(
    *,
    title: str = "Production checkout unavailable",
    description: str = "Customers receive HTTP 503; secret=must-never-leak",
    model_actor_user_id: str | None = "model-forged-user",
    model_assigned_team_id: str | None = "model-forged-team",
) -> TicketCreateRequest:
    return TicketCreateRequest(
        operation_id=str(OPERATION_ID),
        workflow_id=str(WORKFLOW_ID),
        title=title,
        description=description,
        severity=TicketSeverity.P1,
        model_actor_user_id=model_actor_user_id,
        model_assigned_team_id=model_assigned_team_id,
    )


def _service(
    state: MemoryState,
) -> tuple[TicketCreateService, TrustedAssignmentService]:
    factory: Callable[[], OperationUnitOfWork] = MemoryUowFactory(state)
    ledger = OperationLedgerService(uow_factory=factory, clock=lambda: NOW)
    assignments = TrustedAssignmentService()
    service = TicketCreateService(
        operation_ledger=ledger,
        assignment_service=assignments,
        sla_policy=FixedSlaPolicy(),
        clock=lambda: NOW,
        ticket_id_factory=lambda: TICKET_ID,
        ticket_key_factory=lambda: "INC-000001",
        ticket_event_id_factory=lambda: TICKET_EVENT_ID,
        audit_event_id_factory=lambda: AUDIT_EVENT_ID,
        outbox_id_factory=lambda: OUTBOX_ID,
        message_id_factory=lambda: MESSAGE_ID,
    )
    return service, assignments


async def test_p1_create_persists_all_linked_facts_in_one_committed_operation() -> None:
    state = MemoryState()
    service, _ = _service(state)

    result = await service.create(_request(), context=_context(), lease_owner="worker-1")

    assert isinstance(result, TicketCreateResult)
    assert result.ticket_id == str(TICKET_ID)
    assert result.ticket_key == "INC-000001"
    assert result.ticket_version == 1
    assert result.operation_id == str(OPERATION_ID)
    assert result.message_id == str(MESSAGE_ID)
    assert result.replayed is False
    assert state.business_counts() == (1, 1, 1, 1, 1)
    assert state.commits == 1

    ticket = state.tickets[0]
    assert ticket.id == str(TICKET_ID)
    assert ticket.key == "INC-000001"
    assert ticket.version == 1
    assert ticket.severity is TicketSeverity.P1
    assert ticket.created_by_user_id == ACTOR_ID
    assert ticket.assigned_team_id == TRUSTED_TEAM_ID
    assert ticket.sla_deadline == NOW + timedelta(minutes=15)
    assert ticket.sla_version == FixedSlaPolicy.version

    ticket_event = state.ticket_events[0]
    assert ticket_event.id == str(TICKET_EVENT_ID)
    assert ticket_event.ticket_id == str(TICKET_ID)
    assert ticket_event.ticket_version == 1
    assert ticket_event.event_type == "ticket.created"
    assert ticket_event.actor_user_id == ACTOR_ID
    assert ticket_event.operation_id == str(OPERATION_ID)

    audit = state.audit_events[0]
    assert audit.id == str(AUDIT_EVENT_ID)
    assert audit.actor_user_id == ACTOR_ID
    assert audit.actor_session_id == ACTOR_SESSION_ID
    assert audit.workflow_id == str(WORKFLOW_ID)
    assert audit.ticket_id == str(TICKET_ID)
    assert audit.operation_id == str(OPERATION_ID)
    assert audit.message_id == str(MESSAGE_ID)
    assert audit.action == "ticket.create"

    projection = state.workflow_projections[0]
    assert projection.workflow_id == str(WORKFLOW_ID)
    assert projection.ticket_id == str(TICKET_ID)
    assert projection.ticket_version == 1
    assert projection.operation_id == str(OPERATION_ID)

    outbox = state.outbox_events[0]
    assert outbox.id == str(OUTBOX_ID)
    assert outbox.message_id == str(MESSAGE_ID)
    assert outbox.event_type == "ticket.created"
    assert outbox.aggregate_id == str(TICKET_ID)
    assert outbox.operation_id == str(OPERATION_ID)
    assert outbox.payload["ticket_id"] == str(TICKET_ID)
    assert outbox.payload["ticket_version"] == 1


async def test_same_request_replays_original_result_without_new_business_writes() -> None:
    state = MemoryState()
    service, _ = _service(state)

    first = await service.create(_request(), context=_context(), lease_owner="worker-1")
    counts_after_first = state.business_counts()
    second = await service.create(_request(), context=_context(), lease_owner="worker-2")

    assert second.replayed is True
    assert second.ticket_id == first.ticket_id
    assert second.ticket_key == first.ticket_key
    assert second.ticket_version == first.ticket_version
    assert second.operation_id == first.operation_id
    assert second.message_id == first.message_id
    assert state.business_counts() == counts_after_first
    assert len(state.operations) == 1


async def test_concurrent_same_request_creates_one_ticket_and_one_message_identity() -> None:
    state = MemoryState()
    service, _ = _service(state)
    request = _request()

    first, second = await asyncio.gather(
        service.create(request, context=_context(), lease_owner="worker-1"),
        service.create(request, context=_context(), lease_owner="worker-2"),
    )

    assert state.business_counts() == (1, 1, 1, 1, 1)
    assert {first.ticket_id, second.ticket_id} == {str(TICKET_ID)}
    assert {first.message_id, second.message_id} == {str(MESSAGE_ID)}
    assert sum((first.replayed, second.replayed)) == 1


async def test_same_operation_with_different_business_payload_is_rejected() -> None:
    state = MemoryState()
    service, _ = _service(state)
    await service.create(_request(), context=_context(), lease_owner="worker-1")
    before = state.business_counts()

    with pytest.raises(KnowFlowError) as caught:
        await service.create(
            _request(title="A different incident"),
            context=_context(),
            lease_owner="worker-2",
        )

    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert caught.value.retryable is False
    assert state.business_counts() == before


async def test_outbox_failure_rolls_back_ticket_history_audit_projection_and_ledger() -> None:
    state = MemoryState()
    state.fail_outbox = True
    service, _ = _service(state)

    with pytest.raises(RuntimeError, match="injected outbox write failure"):
        await service.create(_request(), context=_context(), lease_owner="worker-1")

    assert state.business_counts() == (0, 0, 0, 0, 0)
    assert state.operations == {}
    assert state.commits == 0


async def test_actor_and_team_are_server_derived_and_model_claims_are_non_authoritative() -> None:
    state = MemoryState()
    service, assignments = _service(state)

    result = await service.create(
        _request(
            model_actor_user_id="admin-from-model",
            model_assigned_team_id="privileged-team-from-model",
        ),
        context=_context(),
        lease_owner="worker-1",
    )

    assert result.ticket_id == str(TICKET_ID)
    assert assignments.contexts == [_context()]
    assert state.tickets[0].created_by_user_id == ACTOR_ID
    assert state.tickets[0].assigned_team_id == TRUSTED_TEAM_ID
    rendered = repr(
        (
            state.tickets,
            state.ticket_events,
            state.audit_events,
            state.workflow_projections,
            state.outbox_events,
        )
    )
    assert "admin-from-model" not in rendered
    assert "privileged-team-from-model" not in rendered


async def test_outbox_audit_and_ledger_summaries_exclude_description_and_secrets() -> None:
    state = MemoryState()
    service, _ = _service(state)
    sensitive_description = (
        "Customer account 4242 is unavailable; password=hunter2 Authorization: Bearer private-token"
    )

    await service.create(
        _request(description=sensitive_description),
        context=_context(),
        lease_owner="worker-1",
    )

    durable_summary = repr(
        (
            state.ticket_events[0].after_summary,
            state.audit_events[0].redacted_metadata,
            state.workflow_projections[0],
            state.outbox_events[0].payload,
            next(iter(state.operations.values())).result_summary,
        )
    )
    assert sensitive_description not in durable_summary
    assert "hunter2" not in durable_summary
    assert "private-token" not in durable_summary
    assert "Authorization" not in durable_summary
    assert "description" not in durable_summary.lower()


def test_repository_and_policy_fakes_satisfy_the_declared_ports() -> None:
    """Keep T037's adapter boundary explicit for its implementation follow-up."""

    state = MemoryState()
    uow = MemoryUnitOfWork(state)
    assignment: TicketAssignmentService = TrustedAssignmentService()
    sla_policy: SlaPolicy = FixedSlaPolicy()

    assert uow.operations is not None
    assert uow.tickets is not None
    assert uow.ticket_events is not None
    assert uow.audit_events is not None
    assert uow.workflow_projections is not None
    assert uow.outbox is not None
    assert assignment is not None
    assert sla_policy is not None
