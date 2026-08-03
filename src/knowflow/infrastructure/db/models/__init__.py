"""Single SQLAlchemy metadata registry for every durable KnowFlow entity."""

from knowflow.infrastructure.db.models.identity import (
    AuditEvent,
    LoginSession,
    Role,
    Team,
    User,
    UserRole,
)
from knowflow.infrastructure.db.models.knowledge import (
    Document,
    DocumentACLGrant,
    DocumentSegment,
    DocumentVersion,
    EvaluationResult,
    EvaluationRun,
    RetrievalEvidence,
)
from knowflow.infrastructure.db.models.ticketing import (
    Approval,
    NotificationDelivery,
    Ticket,
    TicketEvent,
)
from knowflow.infrastructure.db.models.workflow import (
    InboxMessage,
    OperationRecord,
    OutboxEvent,
    PlanDependency,
    PlanTask,
    Workflow,
    WorkflowCommand,
    WorkflowPlan,
)
from knowflow.infrastructure.db.session import Base

__all__ = [
    "Approval",
    "AuditEvent",
    "Base",
    "Document",
    "DocumentACLGrant",
    "DocumentSegment",
    "DocumentVersion",
    "EvaluationResult",
    "EvaluationRun",
    "InboxMessage",
    "LoginSession",
    "NotificationDelivery",
    "OperationRecord",
    "OutboxEvent",
    "PlanDependency",
    "PlanTask",
    "RetrievalEvidence",
    "Role",
    "Team",
    "Ticket",
    "TicketEvent",
    "User",
    "UserRole",
    "Workflow",
    "WorkflowCommand",
    "WorkflowPlan",
]
