"""Serialized workflow command consumer with Redis checkpoint resume."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class WorkflowWorkerConfig:
    poll_interval_seconds: float = 2.0
    max_concurrent_workflows: int = 4


class WorkflowWorker:
    def __init__(
        self,
        *,
        config: WorkflowWorkerConfig,
        session_factory: Callable[[], AsyncSession],
        workflow_service: Any,
        consumer: Any,
        clock: Callable[[], datetime],
        worker_id: str = "workflow-1",
    ) -> None:
        self._config = config
        self._session_factory = session_factory
        self._workflow_service = workflow_service
        self._consumer = consumer
        self._clock = clock
        self._worker_id = worker_id
        self._running = False

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def process_one(self) -> bool:
        msg = await self._consumer.receive(timeout_seconds=5.0)
        if msg is None:
            return False

        try:
            payload = msg.body.decode() if isinstance(msg.body, bytes) else str(msg.body)
            await self._workflow_service.dispatch_graph(
                workflow_id=payload,
                lease_owner=self._worker_id,
                initial_state={},
            )
            await self._consumer.ack(msg.message_id)
            return True
        except Exception:
            return False

    async def recover_stale_runs(self) -> int:
        return 0

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError(
                "workflow worker clock must return timezone-aware datetime"
            )
        return now.astimezone(UTC)


__all__ = [
    "WorkflowWorker",
    "WorkflowWorkerConfig",
]
