"""Short-transaction Outbox leasing with retry, dead-letter, and lease recovery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from knowflow.infrastructure.db.models.workflow import OutboxEvent, OutboxStatus


@dataclass(frozen=True, slots=True)
class OutboxWorkerConfig:
    poll_interval_seconds: float = 1.0
    lease_duration_seconds: float = 30.0
    max_attempts: int = 5
    batch_size: int = 10


class OutboxWorker:
    def __init__(
        self,
        *,
        config: OutboxWorkerConfig,
        session_factory: Callable[[], AsyncSession],
        producer: Any,
        clock: Callable[[], datetime],
        worker_id: str = "outbox-1",
    ) -> None:
        self._config = config
        self._session_factory = session_factory
        self._producer = producer
        self._clock = clock
        self._worker_id = worker_id
        self._running = False

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def process_batch(self) -> int:
        now = self._now()
        async with self._session_factory() as session:
            stmt = (
                select(OutboxEvent)
                .where(
                    OutboxEvent.status.in_(
                        [OutboxStatus.PENDING, OutboxStatus.SENDING]
                    ),
                    OutboxEvent.next_attempt_at <= now,
                )
                .order_by(OutboxEvent.next_attempt_at.asc())
                .limit(self._config.batch_size)
                .with_for_update(skip_locked=True)
            )
            result = await session.execute(stmt)
            events = result.scalars().all()

            for event in events:
                event.status = OutboxStatus.SENDING
                event.lease_owner = self._worker_id
                event.lease_until = now + timedelta(
                    seconds=self._config.lease_duration_seconds
                )
                event.updated_at = now

            await session.commit()
            processed = 0

            for event in events:
                try:
                    mid = await self._producer.send(
                        topic=event.aggregate_type,
                        body=str(event.payload).encode(),
                        message_id=event.message_id,
                    )
                    event.status = OutboxStatus.SENT
                    event.broker_receipt = mid
                    event.sent_at = self._now()
                    processed += 1
                except Exception:
                    event.attempt_count += 1
                    if event.attempt_count >= self._config.max_attempts:
                        event.status = OutboxStatus.DEAD
                        event.error_summary = "Max attempts exceeded"
                    else:
                        event.status = OutboxStatus.PENDING
                        event.next_attempt_at = self._now() + timedelta(
                            seconds=min(
                                2 ** event.attempt_count, 300
                            )
                        )
                    event.lease_owner = None
                    event.lease_until = None

                event.updated_at = self._now()

            await session.commit()
            return processed

    async def recover_leases(self) -> int:
        now = self._now()
        async with self._session_factory() as session:
            stmt = (
                update(OutboxEvent)
                .where(
                    OutboxEvent.status == OutboxStatus.SENDING,
                    OutboxEvent.lease_until < now,
                )
                .values(
                    status=OutboxStatus.PENDING,
                    next_attempt_at=now,
                    lease_owner=None,
                    lease_until=None,
                    updated_at=now,
                )
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount or 0

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("outbox clock must return timezone-aware datetime")
        return now.astimezone(UTC)


__all__ = [
    "OutboxWorker",
    "OutboxWorkerConfig",
]
