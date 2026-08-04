"""Monotonically sequenced workflow events for SSE reconnect."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class WorkflowEvent:
    sequence: int
    event_type: str
    workflow_id: str
    payload: dict[str, Any]
    created_at: datetime


class WorkflowEventStore:
    def __init__(
        self,
        *,
        session_factory: Callable[[], AsyncSession],
        clock: Callable[[], datetime],
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def append_event(
        self,
        *,
        workflow_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> WorkflowEvent:
        from knowflow.infrastructure.db.models.workflow import WorkflowCommand

        now = self._now()
        async with self._session_factory() as session:
            seq = int(now.timestamp())
            command = WorkflowCommand(
                id=f"event-{workflow_id}-{seq}",
                workflow_id=workflow_id,
                sequence=seq,
                kind="USER_MESSAGE",
                payload_hash="0" * 64,
                payload={"event_type": event_type, **payload},
                status="APPLIED",
                created_at=now,
                updated_at=now,
                applied_at=now,
            )
            session.add(command)
            await session.flush()
            return WorkflowEvent(
                sequence=command.sequence,
                event_type=event_type,
                workflow_id=workflow_id,
                payload={"event_type": event_type, **payload},
                created_at=now,
            )

    async def replay_events(
        self,
        *,
        workflow_id: str,
        after_sequence: int | None = None,
        limit: int = 50,
    ) -> tuple[list[WorkflowEvent], int | None]:
        from knowflow.infrastructure.db.models.workflow import WorkflowCommand

        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        async with self._session_factory() as session:
            stmt = (
                select(WorkflowCommand)
                .where(WorkflowCommand.workflow_id == workflow_id)
                .order_by(WorkflowCommand.sequence.asc())
            )
            if after_sequence is not None:
                stmt = stmt.where(
                    WorkflowCommand.sequence > after_sequence
                )
            stmt = stmt.limit(limit + 1)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            items = rows[:limit]
            events = [
                WorkflowEvent(
                    sequence=cmd.sequence,
                    event_type=cmd.payload.get("event_type", "unknown"),
                    workflow_id=workflow_id,
                    payload=cmd.payload,
                    created_at=cmd.created_at,
                )
                for cmd in items
            ]
            last_seq = events[-1].sequence if events else None
            return events, last_seq

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError(
                "event clock must return timezone-aware datetime"
            )
        return now.astimezone(UTC)


__all__ = [
    "WorkflowEvent",
    "WorkflowEventStore",
]
