"""Ticket, approval, event-history, and notification-delivery mappings."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from knowflow.infrastructure.db.session import Base, UTCDateTime, utc_now


def _uuid() -> str:
    return str(uuid4())


def _enum(enum_type: type[StrEnum], name: str) -> Enum:
    return Enum(
        enum_type,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        length=max(len(item.value) for item in enum_type),
    )


class TicketSeverity(StrEnum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class TicketStatus(StrEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


class NotificationDeliveryStatus(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    DELIVERED = "DELIVERED"
    RETRYING = "RETRYING"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


class NotificationChannel(StrEnum):
    EMAIL_SANDBOX = "EMAIL_SANDBOX"


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="ticket_title_length"),
        CheckConstraint(
            "char_length(description) BETWEEN 1 AND 10000", name="ticket_description_length"
        ),
        CheckConstraint("version >= 1", name="ticket_version_positive"),
        CheckConstraint("sla_version >= 0", name="ticket_sla_version_nonnegative"),
        CheckConstraint("escalation_level >= 0", name="ticket_escalation_nonnegative"),
        Index("ix_tickets_creator_status", "created_by_user_id", "status"),
        Index("ix_tickets_team_status", "assigned_team_id", "status"),
        Index("ix_tickets_sla_due", "status", "sla_deadline"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    key: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[TicketSeverity] = mapped_column(
        _enum(TicketSeverity, "ticket_severity"), nullable=False
    )
    status: Mapped[TicketStatus] = mapped_column(
        _enum(TicketStatus, "ticket_status"), nullable=False, default=TicketStatus.OPEN
    )
    created_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    assigned_team_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    assignee_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sla_deadline: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    sla_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    escalation_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )


class TicketEvent(Base):
    """Append-only ticket business history written in the ticket transaction."""

    __tablename__ = "ticket_events"
    __table_args__ = (
        UniqueConstraint(
            "ticket_id", "operation_id", "event_type", name="uq_ticket_event_operation_type"
        ),
        CheckConstraint("ticket_version >= 1", name="ticket_event_version_positive"),
        Index("ix_ticket_events_timeline", "ticket_id", "ticket_version", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ticket_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    ticket_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    operation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("operation_ledger.operation_id", ondelete="SET NULL"), nullable=True
    )
    before_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


@event.listens_for(TicketEvent, "before_update")
@event.listens_for(TicketEvent, "before_delete")
def _reject_ticket_event_mutation(*_: object) -> None:
    raise RuntimeError("ticket events are append-only")


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "plan_version"],
            ["workflow_plans.id", "workflow_plans.version"],
            ondelete="RESTRICT",
            name="fk_approvals_plan",
        ),
        ForeignKeyConstraint(
            ["plan_id", "plan_version", "task_id"],
            ["plan_tasks.plan_id", "plan_tasks.plan_version", "plan_tasks.id"],
            ondelete="RESTRICT",
            name="fk_approvals_task",
        ),
        CheckConstraint("plan_version >= 1", name="approval_plan_version_positive"),
        CheckConstraint("version >= 1", name="approval_version_positive"),
        Index("ix_approvals_pending", "status", "expires_at"),
        Index("ix_approvals_workflow", "workflow_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    workflow_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[str] = mapped_column(String(36), nullable=False)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("operation_ledger.operation_id", ondelete="RESTRICT"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    normalized_parameters: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requester_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    approver_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[ApprovalStatus] = mapped_column(
        _enum(ApprovalStatus, "approval_status"),
        nullable=False,
        default=ApprovalStatus.PENDING,
    )
    decision_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )


class NotificationDelivery(Base):
    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint("attempt_count >= 0", name="notification_attempt_nonnegative"),
        CheckConstraint("template_version >= 1", name="notification_template_version_positive"),
        CheckConstraint(
            "workflow_id IS NOT NULL OR ticket_id IS NOT NULL",
            name="notification_has_parent",
        ),
        Index("ix_notification_delivery_status_due", "status", "next_attempt_at"),
        Index("ix_notification_workflow", "workflow_id", "created_at"),
        Index("ix_notification_ticket", "ticket_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    operation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("operation_ledger.operation_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    source_message_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("outbox_events.message_id", ondelete="RESTRICT"), nullable=False
    )
    workflow_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=True
    )
    ticket_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tickets.id", ondelete="CASCADE"), nullable=True
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        _enum(NotificationChannel, "notification_channel"), nullable=False
    )
    recipient_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    content_template: Mapped[str] = mapped_column(String(128), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    template_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[NotificationDeliveryStatus] = mapped_column(
        _enum(NotificationDeliveryStatus, "notification_delivery_status"),
        nullable=False,
        default=NotificationDeliveryStatus.PENDING,
    )
    provider_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    unknown_since: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )
