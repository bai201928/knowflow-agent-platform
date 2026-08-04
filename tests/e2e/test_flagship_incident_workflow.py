"""Red E2E specification for KnowFlow's canonical incident workflow.

The scenario intentionally crosses a deterministic in-memory harness boundary instead
of replacing the workflow with assertions over test-owned state.  The production slice
must provide ``create_flagship_incident_harness`` before this test can become green.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, cast

import pytest

from knowflow.application.auth.policy import AccessContext
from knowflow.config import Settings

CANONICAL_REQUEST = (
    "查询 RocketMQ 消息积压处理手册，创建一个 P1 工单并通知值班人员；"  # noqa: RUF001
    "调用消费者重启工具前，需要我审批。"  # noqa: RUF001
)
SUBMIT_KEY = "e2e-flagship-submit-v1"
APPROVAL_KEY = "e2e-flagship-approval-v1"
RESUME_KEY = "e2e-flagship-resume-v1"

JsonObject = Mapping[str, Any]


class FlagshipIncidentHarness(Protocol):
    """Deterministic boundary implemented by the real application composition."""

    async def submit(
        self,
        *,
        actor: AccessContext,
        message: str,
        idempotency_key: str,
    ) -> JsonObject: ...

    async def advance_until_blocked(self, workflow_id: str) -> None: ...

    async def approve(
        self,
        *,
        actor: AccessContext,
        approval_id: str,
        expected_version: int,
        idempotency_key: str,
    ) -> JsonObject: ...

    async def resume(
        self,
        *,
        workflow_id: str,
        idempotency_key: str,
    ) -> JsonObject: ...

    async def advance_until_terminal(self, workflow_id: str) -> None: ...

    async def snapshot(self, workflow_id: str) -> JsonObject: ...


@pytest.fixture
def flagship_incident_harness(
    test_settings: Settings,
    clock: Any,
    deterministic_model: Any,
) -> FlagshipIncidentHarness:
    """Load the application-owned memory composition; never emulate it in this test."""

    try:
        from knowflow.infrastructure.testing.flagship_harness import (
            create_flagship_incident_harness,
        )
    except ModuleNotFoundError as exc:
        if exc.name == "knowflow.infrastructure.testing.flagship_harness":
            pytest.fail(
                "T028 RED: implement the deterministic flagship harness at "
                "knowflow.infrastructure.testing.flagship_harness; the E2E must not pass "
                "against a test-owned fake"
            )
        raise

    harness = create_flagship_incident_harness(
        settings=test_settings,
        clock=clock,
        model=deterministic_model,
    )
    return cast(FlagshipIncidentHarness, harness)


def _only(items: Sequence[JsonObject], label: str) -> JsonObject:
    assert len(items) == 1, f"expected exactly one {label}, got {len(items)}"
    return items[0]


def _assert_cited_answer(answer: JsonObject) -> None:
    assert answer["disposition"] == "ANSWERED"
    assert answer["answer"].strip()
    citation = _only(answer["citations"], "citation")
    assert citation["document_id"]
    assert citation["document_version_id"]
    assert citation["segment_id"]
    assert citation["source_location"]


def _assert_stable_audit_timeline(events: Sequence[JsonObject]) -> None:
    sequences = [event["sequence"] for event in events]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    actions = {event["action"] for event in events}
    assert {
        "workflow.accepted",
        "ticket.created",
        "notification.created",
        "approval.requested",
        "approval.approved",
        "operation.succeeded",
        "workflow.succeeded",
    } <= actions


@pytest.mark.asyncio
async def test_canonical_incident_is_exactly_once_across_submit_approval_and_resume_replays(
    flagship_incident_harness: FlagshipIncidentHarness,
    employee_context: AccessContext,
    approver_context: AccessContext,
) -> None:
    """One request reaches one fully linked terminal result despite every replay."""

    first_submit = await flagship_incident_harness.submit(
        actor=employee_context,
        message=CANONICAL_REQUEST,
        idempotency_key=SUBMIT_KEY,
    )
    replayed_submit = await flagship_incident_harness.submit(
        actor=employee_context,
        message=CANONICAL_REQUEST,
        idempotency_key=SUBMIT_KEY,
    )

    assert first_submit["replayed"] is False
    assert replayed_submit["replayed"] is True
    assert replayed_submit["workflow_id"] == first_submit["workflow_id"]
    workflow_id = first_submit["workflow_id"]

    await flagship_incident_harness.advance_until_blocked(workflow_id)
    blocked = await flagship_incident_harness.snapshot(workflow_id)

    assert blocked["status"] == "WAITING_APPROVAL"
    _assert_cited_answer(_only(blocked["answers"], "knowledge answer"))
    ticket = _only(blocked["tickets"], "ticket")
    assert ticket["severity"] == "P1"
    notification = _only(blocked["notifications"], "logical notification")
    assert notification["ticket_id"] == ticket["id"]
    approval = _only(blocked["approvals"], "approval")
    assert approval["status"] == "PENDING"
    assert approval["plan_id"] == blocked["plan"]["id"]
    assert approval["plan_version"] == blocked["plan"]["version"]
    assert approval["task_id"] in {task["id"] for task in blocked["plan"]["tasks"]}
    assert approval["action_type"] == "consumer.restart.sandbox"
    assert approval["resource_type"] == "sandbox_consumer"
    assert approval["resource_id"]
    assert approval["normalized_parameters"]
    assert approval["payload_hash"]
    assert blocked["operations"] == []
    assert blocked["notification_summary"]["total"] == 1
    assert (
        sum(
            blocked["notification_summary"][status]
            for status in ("pending", "delivered", "retrying", "unknown", "failed")
        )
        == 1
    )

    first_decision = await flagship_incident_harness.approve(
        actor=approver_context,
        approval_id=approval["id"],
        expected_version=approval["version"],
        idempotency_key=APPROVAL_KEY,
    )
    replayed_decision = await flagship_incident_harness.approve(
        actor=approver_context,
        approval_id=approval["id"],
        expected_version=approval["version"],
        idempotency_key=APPROVAL_KEY,
    )

    assert first_decision["status"] == "APPROVED"
    assert first_decision["replayed"] is False
    assert replayed_decision["replayed"] is True
    assert replayed_decision["decision_id"] == first_decision["decision_id"]
    assert replayed_decision["decision_version"] == first_decision["decision_version"]

    first_resume = await flagship_incident_harness.resume(
        workflow_id=workflow_id,
        idempotency_key=RESUME_KEY,
    )
    replayed_resume = await flagship_incident_harness.resume(
        workflow_id=workflow_id,
        idempotency_key=RESUME_KEY,
    )
    assert first_resume["replayed"] is False
    assert replayed_resume["replayed"] is True
    assert replayed_resume["command_id"] == first_resume["command_id"]

    await flagship_incident_harness.advance_until_terminal(workflow_id)
    terminal = await flagship_incident_harness.snapshot(workflow_id)

    assert terminal["status"] == "SUCCEEDED"
    assert len(terminal["tickets"]) == 1
    assert len(terminal["notifications"]) == 1
    assert terminal["notification_summary"]["total"] == 1
    assert terminal["notification_summary"]["delivered"] == 1
    decided_approval = _only(terminal["approvals"], "decided approval")
    operation = _only(terminal["operations"], "sandbox operation")
    assert decided_approval["status"] == "APPROVED"
    assert operation["status"] == "SUCCEEDED"
    for field in (
        "operation_id",
        "action_type",
        "resource_type",
        "resource_id",
        "normalized_parameters",
        "payload_hash",
    ):
        assert operation[field] == decided_approval[field]

    summary = terminal["summary"]
    assert summary["citation_ids"] == [terminal["answers"][0]["citations"][0]["segment_id"]]
    assert summary["ticket_ids"] == [ticket["id"]]
    assert summary["notification_ids"] == [notification["id"]]
    assert summary["approval_ids"] == [approval["id"]]
    assert summary["operation_ids"] == [operation["operation_id"]]
    _assert_stable_audit_timeline(terminal["audit_events"])
    assert summary["audit_event_count"] == len(terminal["audit_events"])
