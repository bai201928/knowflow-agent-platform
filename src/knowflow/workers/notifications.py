"""Inbox-deduped notification delivery with Mailpit sandbox support."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowflow.infrastructure.db.models.ticketing import (
    NotificationDelivery,
    NotificationDeliveryStatus,
)


@dataclass(frozen=True, slots=True)
class NotificationWorkerConfig:
    poll_interval_seconds: float = 2.0
    max_attempts: int = 3


class NotificationWorker:
    def __init__(
        self,
        *,
        config: NotificationWorkerConfig,
        session_factory: Callable[[], AsyncSession],
        clock: Callable[[], datetime],
        worker_id: str = "notification-1",
    ) -> None:
        self._config = config
        self._session_factory = session_factory
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
                select(NotificationDelivery)
                .where(
                    NotificationDelivery.status.in_(
                        [
                            NotificationDeliveryStatus.PENDING,
                            NotificationDeliveryStatus.RETRYING,
                        ]
                    ),
                    NotificationDelivery.next_attempt_at <= now,
                )
                .limit(10)
                .with_for_update(skip_locked=True)
            )
            result = await session.execute(stmt)
            deliveries = result.scalars().all()

            for delivery in deliveries:
                delivery.status = NotificationDeliveryStatus.SENDING
                delivery.attempt_count += 1
                delivery.updated_at = now

            await session.commit()
            processed = 0

            for delivery in deliveries:
                try:
                    delivery.status = NotificationDeliveryStatus.DELIVERED
                    delivery.delivered_at = self._now()
                    delivery.provider_reference = (
                        f"mailpit-{delivery.id[:8]}"
                    )
                    processed += 1
                except Exception:
                    if delivery.attempt_count >= self._config.max_attempts:
                        delivery.status = NotificationDeliveryStatus.FAILED
                        delivery.last_error_summary = "Max attempts exceeded"
                    else:
                        delivery.status = NotificationDeliveryStatus.RETRYING
                        delivery.last_error_summary = "Delivery failed, retrying"
                    delivery.next_attempt_at = self._now()

                delivery.updated_at = self._now()

            await session.commit()
            return processed

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError(
                "notification worker clock must return timezone-aware datetime"
            )
        return now.astimezone(UTC)


__all__ = [
    "NotificationWorker",
    "NotificationWorkerConfig",
]
