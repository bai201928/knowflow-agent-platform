from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, Self

from knowflow.application.auth.policy import AccessContext
from knowflow.application.workflows.operations import (
    EffectResult,
    OperationLedgerService,
    OperationRequest,
    OperationUnitOfWork,
)
from knowflow.domain.common.errors import concealed_not_found
from knowflow.domain.common.identity import payload_hash
from knowflow.infrastructure.db.models.ticketing import (
    NotificationChannel,
    NotificationDelivery,
    NotificationDeliveryStatus,
)


@dataclass(frozen=True, slots=True)
class NotificationRegistrationRequest:
    operation_id: str
    source_message_id: str
    channel: NotificationChannel
    recipient_scope: str
    content_template: str
    template_version: int
    template_data: dict[str, Any] = field(default_factory=dict)
    workflow_id: str | None = None
    ticket_id: str | None = None


@dataclass(frozen=True, slots=True)
class RecipientScope:
    scope_type: str
    scope_value: str


@dataclass(frozen=True, slots=True)
class NotificationResult:
    notification_id: str
    operation_id: str
    status: str
    replayed: bool = False


class NotificationRepository(Protocol):
    async def add(self, delivery: NotificationDelivery) -> None: ...
    async def get_by_operation(
        self, operation_id: str
    ) -> NotificationDelivery | None: ...


class NotificationUnitOfWork(OperationUnitOfWork, Protocol):
    notifications: NotificationRepository
    async def __aenter__(self) -> Self: ...


NotificationUnitOfWorkFactory = Callable[[], NotificationUnitOfWork]
Clock = Callable[[], datetime]

_VALID_SCOPE_TYPES = frozenset({"team", "user", "role", "broadcast"})


class NotificationService:
    def __init__(
        self,
        *,
        operation_ledger: OperationLedgerService,
        uow_factory: NotificationUnitOfWorkFactory,
        clock: Clock,
    ) -> None:
        self._ledger = operation_ledger
        self._uow_factory = uow_factory
        self._clock = clock

    async def register_notification(
        self,
        request: NotificationRegistrationRequest,
        *,
        context: AccessContext,
        lease_owner: str,
    ) -> NotificationResult:
        self._validate_request(request)
        _scopes = self.resolve_recipient_scope(request.recipient_scope)
        operation = OperationRequest(
            operation_id=request.operation_id,
            scope_type="notification",
            scope_id=request.workflow_id or request.ticket_id or "direct",
            operation_type="notification.register",
            payload_hash=payload_hash({
                "channel": request.channel.value,
                "recipient_scope": request.recipient_scope,
                "content_template": request.content_template,
                "template_version": request.template_version,
            }),
        )

        async def persist(uow: OperationUnitOfWork) -> EffectResult:
            nuow = self._cast_uow(uow)
            now = self._now()
            delivery = NotificationDelivery(
                operation_id=request.operation_id,
                source_message_id=request.source_message_id,
                workflow_id=request.workflow_id,
                ticket_id=request.ticket_id,
                channel=request.channel,
                recipient_scope=request.recipient_scope,
                content_template=request.content_template,
                template_version=request.template_version,
                template_data=request.template_data,
                status=NotificationDeliveryStatus.PENDING,
                created_at=now,
            )
            await nuow.notifications.add(delivery)
            return EffectResult(
                resource_type="notification_delivery",
                resource_id=delivery.id,
                resource_version=1,
                result_summary={
                    "notification_id": delivery.id,
                    "operation_id": request.operation_id,
                    "status": NotificationDeliveryStatus.PENDING.value,
                },
            )

        entry = await self._ledger.execute_once(
            operation, lease_owner=lease_owner, effect=persist
        )
        summary = entry.result_summary or {}
        return NotificationResult(
            notification_id=str(
                summary.get("notification_id", entry.resource_id or "")
            ),
            operation_id=request.operation_id,
            status=str(summary.get("status", "")),
            replayed=entry.replayed,
        )

    async def lookup_notification_status(
        self, operation_id: str
    ) -> dict[str, Any]:
        async with self._uow_factory() as uow:
            delivery = await uow.notifications.get_by_operation(operation_id)
            if delivery is None:
                raise concealed_not_found("Notification not found")
            return {
                "notification_id": delivery.id,
                "operation_id": delivery.operation_id,
                "status": delivery.status.value,
                "attempt_count": delivery.attempt_count,
                "provider_reference": delivery.provider_reference,
            }

    @staticmethod
    def resolve_recipient_scope(
        recipient_scope: str,
    ) -> list[RecipientScope]:
        if not recipient_scope or not recipient_scope.strip():
            raise ValueError("recipient_scope must not be empty")
        parts = [p.strip() for p in recipient_scope.split(",") if p.strip()]
        scopes: list[RecipientScope] = []
        for part in parts:
            if ":" not in part:
                raise ValueError(
                    f"Invalid recipient scope format: {part!r}"
                    " (expected type:value)"
                )
            scope_type, _, scope_value = part.partition(":")
            scope_type = scope_type.strip()
            scope_value = scope_value.strip()
            if scope_type not in _VALID_SCOPE_TYPES:
                raise ValueError(
                    f"Unknown scope type {scope_type!r}; "
                    f"allowed: {sorted(_VALID_SCOPE_TYPES)}"
                )
            if not scope_value:
                raise ValueError(
                    f"scope_value is required for {scope_type!r}"
                )
            scopes.append(
                RecipientScope(scope_type=scope_type, scope_value=scope_value)
            )
        if not scopes:
            raise ValueError("recipient_scope resolved to zero recipients")
        return scopes

    @staticmethod
    def _cast_uow(uow: OperationUnitOfWork) -> NotificationUnitOfWork:
        if not hasattr(uow, "notifications"):
            raise TypeError(
                "unit of work does not provide notification repository"
            )
        return uow  # type: ignore[return-value]

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError(
                "notification clock must return timezone-aware datetime"
            )
        return now.astimezone(UTC)

    @staticmethod
    def _validate_request(
        request: NotificationRegistrationRequest,
    ) -> None:
        for f_name, f_value, f_max in (
            ("operation_id", request.operation_id, 128),
            ("source_message_id", request.source_message_id, 128),
            ("recipient_scope", request.recipient_scope, 255),
            ("content_template", request.content_template, 128),
        ):
            if not f_value.strip() or len(f_value) > f_max:
                raise ValueError(
                    f"{f_name} must be between 1 and {f_max} characters"
                )
        if request.template_version < 1:
            raise ValueError("template_version must be positive")
        if request.workflow_id is None and request.ticket_id is None:
            raise ValueError(
                "at least one of workflow_id or ticket_id is required"
            )


__all__ = [
    "NotificationRegistrationRequest",
    "NotificationRepository",
    "NotificationResult",
    "NotificationService",
    "NotificationUnitOfWork",
    "NotificationUnitOfWorkFactory",
    "RecipientScope",
]
