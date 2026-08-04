"""Flagged incident workflow nodes.

Each node function follows the same contract: accept the LangGraph state plus
callable dependencies, return a state update dict.  Nodes that need durable
effects communicate through the application services wired in graph assembly.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

from knowflow.workflows.state import (
    PendingApproval,
    PendingClarification,
    WorkflowState,
)


async def planner_node(
    state: WorkflowState,
    *,
    planner: Any,
    compiler: Any,
    clock: Callable[[], datetime],
    lease_owner: str,
) -> dict[str, Any]:
    """Invoke the model planner and compile a server-validated plan."""
    now = clock()
    planner_result = await planner.plan(
        request_text=state.get("messages", [{}])[-1].get("summary", ""),
        deadline_at=now,
    )
    compile_result = await compiler.compile(
        planner_result=planner_result,
        workflow_id=state["workflow_id"],
        lease_owner=lease_owner,
    )

    pending_clarification = None
    if compile_result.clarification:
        pending_clarification = PendingClarification(
            id=str(uuid4()),
            code=compile_result.clarification.code,
            question=compile_result.clarification.question,
            required_slots=tuple(compile_result.clarification.required_slots),
            plan_version=compile_result.plan_version,
        )

    return {
        "plan_id": compile_result.plan_id,
        "plan_version": compile_result.plan_version,
        "intent_name": compile_result.intent_name,
        "pending_clarification": pending_clarification,
        "status": "WAITING_CLARIFICATION" if pending_clarification else "RUNNING",
        "task_results": [
            {
                "id": str(uuid4()),
                "task_id": "planner",
                "status": "SUCCEEDED" if not pending_clarification else "PENDING",
                "result_ref": compile_result.plan_id,
                "summary": "Plan compiled" if not pending_clarification else "Clarification needed",
                "error_code": None,
            }
        ],
    }


async def clarification_node(
    state: WorkflowState,
) -> dict[str, Any]:
    """Present a clarification question and wait for user response."""
    clarification = state.get("pending_clarification")
    if clarification is None:
        return {"status": "RUNNING"}

    return {
        "status": "WAITING_CLARIFICATION",
        "messages": [
            {
                "id": str(uuid4()),
                "role": "assistant",
                "summary": clarification["question"][:512],
                "payload_hash": clarification["code"],
            }
        ],
    }


async def retrieval_node(
    state: WorkflowState,
    *,
    retrieval_service: Any,
    access_context: Any,
) -> dict[str, Any]:
    """Retrieve authorized knowledge evidence for the current plan."""
    query_text = state.get("messages", [{}])[-1].get("summary", "")
    result = await retrieval_service.retrieve(
        query=query_text,
        context=access_context,
    )

    evidence_refs = []
    for evidence in result.evidence or []:
        evidence_refs.append({
            "id": str(uuid4()),
            "task_id": "retrieval",
            "document_id": getattr(evidence, "document_id", ""),
            "document_version_id": getattr(evidence, "document_version_id", ""),
            "segment_id": getattr(evidence, "segment_id", ""),
            "content_hash": getattr(evidence, "content_hash", ""),
            "citation_label": getattr(evidence, "citation_label", ""),
        })

    disposition = getattr(result, "disposition", None)
    disposition_value = disposition.value if disposition else "SUFFICIENT"

    return {
        "evidence_refs": evidence_refs,
        "task_results": [
            {
                "id": str(uuid4()),
                "task_id": "retrieval",
                "status": "SUCCEEDED",
                "result_ref": None,
                "summary": f"Retrieved {len(evidence_refs)} segments [{disposition_value}]",
                "error_code": None,
            }
        ],
        "status": "RUNNING",
    }


async def ticket_node(
    state: WorkflowState,
    *,
    ticket_service: Any,
    access_context: Any,
    lease_owner: str,
) -> dict[str, Any]:
    """Create an idempotent ticket for the workflow."""
    result = await ticket_service.create(
        operation_id=str(uuid4()),
        workflow_id=state["workflow_id"],
        title=f"Incident: {state.get('intent_name', 'Unknown')}",
        description=state.get("messages", [{}])[-1].get("summary", ""),
        severity="P2",
        context=access_context,
        lease_owner=lease_owner,
    )

    return {
        "task_results": [
            {
                "id": str(uuid4()),
                "task_id": "ticket",
                "status": "SUCCEEDED",
                "result_ref": result.ticket_id,
                "summary": f"Ticket {result.ticket_key} created",
                "error_code": None,
            }
        ],
        "status": "RUNNING",
    }


async def notification_node(
    state: WorkflowState,
    *,
    notification_service: Any,
    access_context: Any,
    lease_owner: str,
) -> dict[str, Any]:
    """Register a durable notification for the workflow."""
    await notification_service.register_notification(
        operation_id=str(uuid4()),
        source_message_id=str(uuid4()),
        channel="EMAIL_SANDBOX",
        recipient_scope="broadcast:noc",
        content_template="incident-created",
        template_version=1,
        template_data={"workflow_id": state["workflow_id"]},
        workflow_id=state["workflow_id"],
        context=access_context,
        lease_owner=lease_owner,
    )

    return {
        "task_results": [
            {
                "id": str(uuid4()),
                "task_id": "notification",
                "status": "SUCCEEDED",
                "result_ref": None,
                "summary": "Notification registered",
                "error_code": None,
            }
        ],
        "status": "RUNNING",
    }


async def approval_node(
    state: WorkflowState,
    *,
    approval_service: Any,
    access_context: Any,
    lease_owner: str,
    clock: Callable[[], datetime],
) -> dict[str, Any]:
    """Create an approval and pause for human decision."""
    now = clock()
    result = await approval_service.create_approval(
        operation_id=str(uuid4()),
        workflow_id=state["workflow_id"],
        plan_id=state.get("plan_id", ""),
        plan_version=state.get("plan_version", 1),
        task_id="sandbox_ops",
        action_type="consumer_restart",
        resource_type="rocketmq_consumer",
        resource_id="orders-consumer",
        requester_user_id=state["actor_user_id"],
        expires_at=now + __import__("datetime").timedelta(hours=1),
        context=access_context,
        lease_owner=lease_owner,
    )

    pending_approval = PendingApproval(
        id=result.approval_id,
        plan_version=state.get("plan_version", 1),
        required_role="APPROVER",
        expires_at=(now + __import__("datetime").timedelta(hours=1)).isoformat(),
    )

    return {
        "pending_approval": pending_approval,
        "status": "WAITING_APPROVAL",
        "task_results": [
            {
                "id": str(uuid4()),
                "task_id": "approval",
                "status": "PENDING",
                "result_ref": result.approval_id,
                "summary": f"Approval {result.approval_id} created",
                "error_code": None,
            }
        ],
    }


async def sandbox_node(
    state: WorkflowState,
    *,
    sandbox_executor: Any,
    lease_owner: str,
) -> dict[str, Any]:
    """Execute the approved sandbox operation."""
    result = await sandbox_executor.execute_operation(
        operation_id=str(uuid4()),
        operation_type="consumer_restart",
        payload={"consumer_group": "orders-consumer", "action": "restart"},
        lease_owner=lease_owner,
    )

    return {
        "task_results": [
            {
                "id": str(uuid4()),
                "task_id": "sandbox",
                "status": "SUCCEEDED",
                "result_ref": result.operation_id,
                "summary": f"Sandbox op {result.operation_id}: {result.status}",
                "error_code": None,
            }
        ],
        "status": "RUNNING",
    }


async def final_summary_node(
    state: WorkflowState,
    *,
    audit_service: Any,
) -> dict[str, Any]:
    """Produce the final workflow summary and record audit closure."""
    task_results = state.get("task_results", [])
    completed = [t for t in task_results if t.get("status") == "SUCCEEDED"]
    summary = (
        f"Workflow complete. {len(completed)}/{len(task_results)} tasks succeeded."
    )

    await audit_service.write_audit(
        action="workflow.completed",
        resource_type="workflow",
        resource_id=state["workflow_id"],
        outcome="SUCCEEDED",
        metadata={"task_count": len(task_results), "completed_count": len(completed)},
    )

    return {
        "final_summary": summary,
        "status": "SUCCEEDED",
    }


async def approval_resume_node(
    state: WorkflowState,
    *,
    clock: Callable[[], datetime],
) -> dict[str, Any]:
    """Resume workflow execution after an approval decision is received.

    Reads the latest APPROVAL_RESUME command from the commands list, validates
    it against the current plan version, and transitions status back to RUNNING.
    """
    commands = state.get("commands", [])
    resume_cmd = None
    for cmd in reversed(commands):
        if cmd.get("kind") == "APPROVAL_RESUME":
            resume_cmd = cmd
            break

    if resume_cmd is None:
        return {
            "status": "NEEDS_REVIEW",
            "error": {
                "code": "MISSING_APPROVAL_COMMAND",
                "retryable": False,
                "summary": "No APPROVAL_RESUME command found in state",
                "detail_ref": None,
            },
        }

    current_version = state.get("plan_version", 0)
    cmd_version = resume_cmd.get("accepted_version", 0)
    if cmd_version != current_version:
        return {
            "status": "NEEDS_REVIEW",
            "error": {
                "code": "VERSION_MISMATCH",
                "retryable": False,
                "summary": f"Command version {cmd_version} != plan version {current_version}",
                "detail_ref": None,
            },
        }

    now = clock()
    return {
        "pending_approval": None,
        "status": "RUNNING",
        "task_results": [
            {
                "id": str(uuid4()),
                "task_id": "approval_resume",
                "status": "SUCCEEDED",
                "result_ref": resume_cmd.get("id"),
                "summary": f"Approval resumed at {now.isoformat()}",
                "error_code": None,
            }
        ],
        "audit_refs": [
            {
                "id": str(uuid4()),
                "sequence": 0,
                "event_type": "approval.resumed",
            }
        ],
    }


async def error_handler_node(
    state: WorkflowState,
    *,
    clock: Callable[[], datetime],
) -> dict[str, Any]:
    """Handle errors from any workflow node and determine the recovery path.

    Retryable errors transition to NEEDS_REVIEW for operator inspection.
    Non-retryable errors transition to FAILED immediately.
    """
    error = state.get("error")
    if error is None:
        return {"status": "RUNNING"}

    now = clock()
    error_code = error.get("code", "UNKNOWN")
    error_summary = error.get("summary", "")
    is_retryable = error.get("retryable", False)

    if is_retryable:
        return {
            "status": "NEEDS_REVIEW",
            "task_results": [
                {
                    "id": str(uuid4()),
                    "task_id": "error_handler",
                    "status": "PENDING",
                    "result_ref": error.get("detail_ref"),
                    "summary": f"Retryable error: {error_code}",
                    "error_code": error_code,
                }
            ],
            "audit_refs": [
                {
                    "id": str(uuid4()),
                    "sequence": 0,
                    "event_type": "workflow.error.needs_review",
                }
            ],
        }

    return {
        "status": "FAILED",
        "final_summary": (
            f"Workflow failed at {now.isoformat()}: "
            f"[{error_code}] {error_summary[:400]}"
        ),
        "task_results": [
            {
                "id": str(uuid4()),
                "task_id": "error_handler",
                "status": "FAILED",
                "result_ref": None,
                "summary": f"Non-retryable error: {error_code}",
                "error_code": error_code,
            }
        ],
        "audit_refs": [
            {
                "id": str(uuid4()),
                "sequence": 0,
                "event_type": "workflow.error.failed",
            }
        ],
    }



__all__ = [
    "approval_node",
    "approval_resume_node",
    "clarification_node",
    "error_handler_node",
    "final_summary_node",
    "notification_node",
    "planner_node",
    "retrieval_node",
    "sandbox_node",
    "ticket_node",
]
