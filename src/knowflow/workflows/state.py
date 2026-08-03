"""Compact, checkpoint-safe LangGraph state for KnowFlow workflows.

MySQL owns business facts.  This module deliberately keeps only identifiers,
bounded summaries, and references that are sufficient to resume graph execution.
Credentials, document bodies, model prompts/responses, and unbounded tool output
must never be added to this state.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Final, Literal, NotRequired, TypedDict

MAX_STATE_SUMMARY_CHARS: Final = 512
STATE_SCHEMA_VERSION: Final = 1

WorkflowStatus = Literal[
    "ACCEPTED",
    "PLANNING",
    "WAITING_CLARIFICATION",
    "RUNNING",
    "WAITING_APPROVAL",
    "NEEDS_REVIEW",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
]
TaskStatus = Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "SKIPPED", "UNKNOWN"]


class MessageRef(TypedDict):
    """Reference to a durable user/assistant message, never its full body."""

    id: str
    role: Literal["user", "assistant", "system"]
    summary: str
    payload_hash: str


class CommandRef(TypedDict):
    """Reference to a durable workflow command."""

    id: str
    kind: str
    payload_hash: str
    accepted_version: int


class TaskResultRef(TypedDict):
    """Compact task outcome pointing to the authoritative durable result."""

    id: str
    task_id: str
    status: TaskStatus
    result_ref: str | None
    summary: str
    error_code: str | None


class EvidenceRef(TypedDict):
    """Citation-safe reference to persisted retrieval evidence."""

    id: str
    task_id: str
    document_id: str
    document_version_id: str
    segment_id: str
    content_hash: str
    citation_label: str


class AuditRef(TypedDict):
    """Reference to one immutable audit fact."""

    id: str
    sequence: int
    event_type: str


class PendingClarification(TypedDict):
    """Bounded prompt and durable identity for an open clarification barrier."""

    id: str
    code: str
    question: str
    required_slots: tuple[str, ...]
    plan_version: int


class PendingApproval(TypedDict):
    """Binding needed to resume one durable approval safely."""

    id: str
    plan_version: int
    required_role: str
    expires_at: str


class WorkflowError(TypedDict):
    """Safe error projection; detailed diagnostics live outside checkpoints."""

    code: str
    retryable: bool
    summary: str
    detail_ref: str | None


def _bounded(value: str) -> str:
    """Return a deterministic, single-line checkpoint summary."""

    return " ".join(value.split())[:MAX_STATE_SUMMARY_CHARS]


def _merge_id_refs[Ref: (MessageRef, CommandRef, TaskResultRef, EvidenceRef, AuditRef)](
    left: list[Ref], right: list[Ref]
) -> list[Ref]:
    """Merge references by ID without mutating either input.

    The first occurrence fixes an ID's position, while the last occurrence wins
    its value.  This makes replay order stable and lets later durable projections
    refresh an earlier reference.  Missing, blank, or non-string IDs fail fast so
    malformed checkpoint updates cannot silently alias one another.
    """

    order: list[str] = []
    by_id: dict[str, Ref] = {}
    for source in (left, right):
        for item in source:
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id.strip():
                raise TypeError("state reducer items require a non-blank string id")

            copied = deepcopy(item)
            if item_id not in by_id:
                order.append(item_id)
            by_id[item_id] = copied

    return [by_id[item_id] for item_id in order]


def reduce_messages(left: list[MessageRef], right: list[MessageRef]) -> list[MessageRef]:
    bounded_left = [_bounded_message_ref(item) for item in left]
    bounded_right = [_bounded_message_ref(item) for item in right]
    return _merge_id_refs(bounded_left, bounded_right)


def _bounded_message_ref(item: MessageRef) -> MessageRef:
    return MessageRef(
        id=item["id"],
        role=item["role"],
        summary=_bounded(item["summary"]),
        payload_hash=item["payload_hash"],
    )


def reduce_commands(left: list[CommandRef], right: list[CommandRef]) -> list[CommandRef]:
    return _merge_id_refs(left, right)


def reduce_task_results(
    left: list[TaskResultRef], right: list[TaskResultRef]
) -> list[TaskResultRef]:
    bounded_left = [_bounded_task_result_ref(item) for item in left]
    bounded_right = [_bounded_task_result_ref(item) for item in right]
    return _merge_id_refs(bounded_left, bounded_right)


def _bounded_task_result_ref(item: TaskResultRef) -> TaskResultRef:
    return TaskResultRef(
        id=item["id"],
        task_id=item["task_id"],
        status=item["status"],
        result_ref=item["result_ref"],
        summary=_bounded(item["summary"]),
        error_code=item["error_code"],
    )


def reduce_evidence(left: list[EvidenceRef], right: list[EvidenceRef]) -> list[EvidenceRef]:
    return _merge_id_refs(left, right)


def reduce_audit_refs(left: list[AuditRef], right: list[AuditRef]) -> list[AuditRef]:
    return _merge_id_refs(left, right)


# Fields without reducers have exactly one owning writer.  Other nodes may read
# them but must return no update for them.  The graph assembly can use this map
# for validation and documentation without importing node implementations.
SINGLE_VALUE_OWNERS: Final[dict[str, str]] = {
    "workflow_id": "workflow_service",
    "thread_id": "workflow_service",
    "request_id": "workflow_service",
    "actor_user_id": "workflow_service",
    "schema_version": "workflow_service",
    "workflow_version": "workflow_projection",
    "plan_id": "plan_compiler",
    "plan_version": "plan_compiler",
    "intent_name": "planner",
    "pending_clarification": "plan_compiler",
    "pending_approval": "approval_node",
    "deadline_at": "workflow_service",
    "status": "workflow_orchestrator",
    "error": "workflow_orchestrator",
    "final_summary": "final_summary_node",
}


class WorkflowState(TypedDict):
    """Checkpoint schema consumed by the LangGraph workflow.

    Collection fields use replay-safe reducers.  Every optional single-value
    field has the sole writer documented in :data:`SINGLE_VALUE_OWNERS`.
    """

    workflow_id: str
    thread_id: str
    request_id: str
    actor_user_id: str
    schema_version: int
    workflow_version: int
    deadline_at: str
    status: WorkflowStatus

    plan_id: NotRequired[str | None]
    plan_version: NotRequired[int]
    intent_name: NotRequired[str | None]
    pending_clarification: NotRequired[PendingClarification | None]
    pending_approval: NotRequired[PendingApproval | None]
    error: NotRequired[WorkflowError | None]
    final_summary: NotRequired[str | None]

    messages: Annotated[list[MessageRef], reduce_messages]
    commands: Annotated[list[CommandRef], reduce_commands]
    task_results: Annotated[list[TaskResultRef], reduce_task_results]
    evidence_refs: Annotated[list[EvidenceRef], reduce_evidence]
    audit_refs: Annotated[list[AuditRef], reduce_audit_refs]


def initial_state(
    *,
    workflow_id: str,
    thread_id: str,
    request_id: str,
    actor_user_id: str,
    workflow_version: int,
    deadline_at: str,
) -> WorkflowState:
    """Build a complete initial state with fresh collection values."""

    return WorkflowState(
        workflow_id=workflow_id,
        thread_id=thread_id,
        request_id=request_id,
        actor_user_id=actor_user_id,
        schema_version=STATE_SCHEMA_VERSION,
        workflow_version=workflow_version,
        deadline_at=deadline_at,
        status="ACCEPTED",
        messages=[],
        commands=[],
        task_results=[],
        evidence_refs=[],
        audit_refs=[],
    )
