"""Durable workflow acceptance, command dispatch, and graph execution service."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowflow.application.auth.policy import AccessContext
from knowflow.application.workflows.operations import OperationLedgerService
from knowflow.domain.common.errors import concealed_not_found
from knowflow.infrastructure.db.models.workflow import Workflow, WorkflowStatus


@dataclass(frozen=True, slots=True)
class WorkflowCreateRequest:
    operation_id: str
    thread_id: str
    request_text: str
    deadline_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class WorkflowResult:
    workflow_id: str
    thread_id: str
    status: str
    plan_id: str | None = None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowProjection:
    id: str
    thread_id: str
    status: str
    plan_id: str | None
    plan_version: int
    deadline_at: datetime | None
    pending_approval_id: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class WorkflowService:
    def __init__(
        self,
        *,
        operation_ledger: OperationLedgerService,
        session_factory: Callable[[], AsyncSession],
        graph_builder: Callable[..., Any],
        graph_services: dict[str, Any],
        access_context: AccessContext,
        clock: Callable[[], datetime],
    ) -> None:
        self._ledger = operation_ledger
        self._session_factory = session_factory
        self._graph_builder = graph_builder
        self._graph_services = graph_services
        self._access_context = access_context
        self._clock = clock

    async def accept_workflow(
        self,
        request: WorkflowCreateRequest,
        *,
        lease_owner: str,
    ) -> WorkflowResult:
        async with self._session_factory() as session:
            workflow = Workflow(
                thread_id=request.thread_id,
                owner_user_id=self._access_context.user_id,
                session_id=self._access_context.session_id,
                original_request=request.request_text,
                status=WorkflowStatus.RECEIVED,
                deadline_at=request.deadline_at,
                accepted_at=self._now(),
                version=1,
            )
            session.add(workflow)
            await session.flush()
            await session.commit()
            return WorkflowResult(
                workflow_id=workflow.id,
                thread_id=workflow.thread_id,
                status=workflow.status.value,
            )

    async def dispatch_graph(
        self,
        workflow_id: str,
        *,
        lease_owner: str,
        initial_state: dict[str, Any],
    ) -> dict[str, Any]:
        graph = self._graph_builder(
            **self._graph_services,
            lease_owner=lease_owner,
            access_context=self._access_context,
            clock=self._clock,
        )
        result = await graph.ainvoke(initial_state)
        status = result.get("status", "FAILED")

        async with self._session_factory() as session:
            wf = await session.get(Workflow, workflow_id)
            if wf is not None:
                members = WorkflowStatus.__members__
                wf_status = (
                    WorkflowStatus(status)
                    if status in members
                    else WorkflowStatus.FAILED
                )
                wf.status = wf_status
                wf.last_confirmed_stage = "final_summary"
                if status == "SUCCEEDED":
                    wf.completed_at = self._now()
                wf.version += 1
                wf.updated_at = self._now()
                await session.commit()

        return result

    async def get_workflow(
        self, workflow_id: str
    ) -> WorkflowProjection:
        async with self._session_factory() as session:
            wf = await session.get(Workflow, workflow_id)
            if wf is None:
                raise concealed_not_found("Workflow not found")
            return WorkflowProjection(
                id=wf.id,
                thread_id=wf.thread_id,
                status=wf.status.value,
                plan_id=wf.plan_id,
                plan_version=wf.plan_version,
                deadline_at=wf.deadline_at,
                pending_approval_id=wf.pending_approval_id,
                version=wf.version,
                created_at=wf.created_at,
                updated_at=wf.updated_at,
            )

    async def list_workflows(
        self,
        *,
        owner_user_id: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> tuple[list[WorkflowProjection], str | None]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        async with self._session_factory() as session:
            stmt = select(Workflow).order_by(Workflow.created_at.desc())
            if owner_user_id:
                stmt = stmt.where(Workflow.owner_user_id == owner_user_id)
            if status:
                stmt = stmt.where(Workflow.status == status)
            if cursor:
                stmt = stmt.where(Workflow.id < cursor)
            stmt = stmt.limit(limit + 1)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            has_more = len(rows) > limit
            items = rows[:limit]
            projections = [
                WorkflowProjection(
                    id=wf.id,
                    thread_id=wf.thread_id,
                    status=wf.status.value,
                    plan_id=wf.plan_id,
                    plan_version=wf.plan_version,
                    deadline_at=wf.deadline_at,
                    pending_approval_id=wf.pending_approval_id,
                    version=wf.version,
                    created_at=wf.created_at,
                    updated_at=wf.updated_at,
                )
                for wf in items
            ]
            next_cursor = items[-1].id if has_more and items else None
            return projections, next_cursor

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError(
                "workflow clock must return timezone-aware datetime"
            )
        return now.astimezone(UTC)


__all__ = [
    "WorkflowCreateRequest",
    "WorkflowProjection",
    "WorkflowResult",
    "WorkflowService",
]
