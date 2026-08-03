"""Workflow planning, idempotency-ledger, and durable messaging mappings."""

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


class WorkflowStatus(StrEnum):
    RECEIVED = "RECEIVED"
    CLARIFYING = "CLARIFYING"
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_RETRY = "WAITING_RETRY"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class PlanStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    VALIDATED = "VALIDATED"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class IntentType(StrEnum):
    KNOWLEDGE_QUERY = "KNOWLEDGE_QUERY"
    TICKET_CREATE = "TICKET_CREATE"
    TICKET_QUERY = "TICKET_QUERY"
    TICKET_UPDATE = "TICKET_UPDATE"
    NOTIFICATION_SEND = "NOTIFICATION_SEND"
    OPS_ACTION = "OPS_ACTION"


class DependencyKind(StrEnum):
    SEQUENCE = "SEQUENCE"
    DATA = "DATA"
    ON_SUCCESS = "ON_SUCCESS"
    ON_FAILURE = "ON_FAILURE"


class WorkflowCommandKind(StrEnum):
    USER_MESSAGE = "USER_MESSAGE"
    CLARIFICATION = "CLARIFICATION"
    APPROVAL_RESUME = "APPROVAL_RESUME"
    CANCEL = "CANCEL"
    RECOVER = "RECOVER"


class WorkflowCommandStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class OperationStatus(StrEnum):
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    UNKNOWN = "UNKNOWN"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    DEAD = "DEAD"


class InboxStatus(StrEnum):
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    DEAD = "DEAD"


class Workflow(Base):
    __tablename__ = "workflows"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "plan_version"],
            ["workflow_plans.id", "workflow_plans.version"],
            name="fk_workflows_current_plan",
            use_alter=True,
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["pending_approval_id"],
            ["approvals.id"],
            name="fk_workflows_pending_approval",
            use_alter=True,
            ondelete="SET NULL",
        ),
        CheckConstraint("plan_version >= 0", name="workflow_plan_version_nonnegative"),
        CheckConstraint("version >= 1", name="workflow_version_positive"),
        Index("ix_workflows_owner_status", "owner_user_id", "status"),
        Index("ix_workflows_status_updated", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    thread_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, default=_uuid)
    owner_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("login_sessions.id", ondelete="SET NULL"), nullable=True
    )
    original_request: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[WorkflowStatus] = mapped_column(
        _enum(WorkflowStatus, "workflow_status"),
        nullable=False,
        default=WorkflowStatus.RECEIVED,
    )
    plan_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deadline_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    pending_approval_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_operation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_confirmed_stage: Mapped[str | None] = mapped_column(String(128), nullable=True)
    active_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )


class WorkflowPlan(Base):
    __tablename__ = "workflow_plans"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workflow_id"], ["workflows.id"], ondelete="CASCADE", name="fk_plan_workflow"
        ),
        UniqueConstraint("workflow_id", "version", name="uq_workflow_plans_workflow_version"),
        CheckConstraint("version >= 1", name="plan_version_positive"),
        CheckConstraint("schema_version >= 1", name="plan_schema_version_positive"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String(36), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[PlanStatus] = mapped_column(
        _enum(PlanStatus, "workflow_plan_status"), nullable=False, default=PlanStatus.CANDIDATE
    )
    risk_level: Mapped[RiskLevel] = mapped_column(
        _enum(RiskLevel, "risk_level"), nullable=False, default=RiskLevel.LOW
    )
    validation_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class PlanTask(Base):
    __tablename__ = "plan_tasks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "plan_version"],
            ["workflow_plans.id", "workflow_plans.version"],
            ondelete="CASCADE",
            name="fk_plan_tasks_plan",
        ),
        UniqueConstraint("operation_id", name="uq_plan_tasks_operation_id"),
        CheckConstraint("position >= 0", name="plan_task_position_nonnegative"),
    )

    plan_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    intent: Mapped[IntentType] = mapped_column(
        _enum(IntentType, "plan_task_intent"), nullable=False
    )
    source_span: Mapped[str] = mapped_column(Text, nullable=False)
    slots: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    missing_slots: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    risk_level: Mapped[RiskLevel] = mapped_column(
        _enum(RiskLevel, "plan_task_risk_level"), nullable=False, default=RiskLevel.LOW
    )
    operation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class PlanDependency(Base):
    __tablename__ = "plan_dependencies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "plan_version", "from_task_id"],
            ["plan_tasks.plan_id", "plan_tasks.plan_version", "plan_tasks.id"],
            ondelete="CASCADE",
            name="fk_plan_dependencies_from_task",
        ),
        ForeignKeyConstraint(
            ["plan_id", "plan_version", "to_task_id"],
            ["plan_tasks.plan_id", "plan_tasks.plan_version", "plan_tasks.id"],
            ondelete="CASCADE",
            name="fk_plan_dependencies_to_task",
        ),
        CheckConstraint("from_task_id <> to_task_id", name="plan_dependency_not_self"),
    )

    plan_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    to_task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[DependencyKind] = mapped_column(
        _enum(DependencyKind, "plan_dependency_kind"), primary_key=True
    )
    output_binding: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class WorkflowCommand(Base):
    __tablename__ = "workflow_commands"
    __table_args__ = (
        UniqueConstraint("workflow_id", "sequence", name="uq_workflow_commands_sequence"),
        CheckConstraint("sequence >= 1", name="workflow_command_sequence_positive"),
        Index("ix_workflow_commands_pending", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[WorkflowCommandKind] = mapped_column(
        _enum(WorkflowCommandKind, "workflow_command_kind"), nullable=False
    )
    expected_workflow_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[WorkflowCommandStatus] = mapped_column(
        _enum(WorkflowCommandStatus, "workflow_command_status"),
        nullable=False,
        default=WorkflowCommandStatus.PENDING,
    )
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class OperationRecord(Base):
    __tablename__ = "operation_ledger"
    __table_args__ = (
        UniqueConstraint(
            "scope_type",
            "scope_id",
            "operation_type",
            "operation_id",
            name="uq_operation_scope_identity",
        ),
        CheckConstraint("attempt_count >= 0", name="operation_attempt_nonnegative"),
        Index("ix_operation_ledger_status_lease", "status", "lease_until"),
    )

    operation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[OperationStatus] = mapped_column(
        _enum(OperationStatus, "operation_status"),
        nullable=False,
        default=OperationStatus.CLAIMED,
    )
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resource_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint("schema_version >= 1", name="outbox_schema_version_positive"),
        CheckConstraint("attempt_count >= 0", name="outbox_attempt_nonnegative"),
        Index("ix_outbox_due", "status", "next_attempt_at", "lease_until"),
        Index("ix_outbox_aggregate", "aggregate_type", "aggregate_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    message_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("operation_ledger.operation_id", ondelete="SET NULL"), nullable=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    trace_context: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[OutboxStatus] = mapped_column(
        _enum(OutboxStatus, "outbox_status"), nullable=False, default=OutboxStatus.PENDING
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    broker_receipt: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class InboxMessage(Base):
    __tablename__ = "inbox_messages"
    __table_args__ = (
        CheckConstraint("schema_version >= 1", name="inbox_schema_version_positive"),
        CheckConstraint("attempt_count >= 0", name="inbox_attempt_nonnegative"),
        Index("ix_inbox_processing", "status", "updated_at"),
    )

    consumer_group: Mapped[str] = mapped_column(String(128), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[InboxStatus] = mapped_column(
        _enum(InboxStatus, "inbox_status"), nullable=False, default=InboxStatus.PROCESSING
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    local_resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    local_resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    result_summary: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
