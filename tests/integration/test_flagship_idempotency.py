"""T030: flagship replays and concurrent resumes have one business effect."""

from __future__ import annotations

import asyncio
from importlib import import_module
from typing import Protocol, cast

import pytest

from knowflow.domain.common.errors import ErrorCode, KnowFlowError

CANONICAL_REQUEST = (
    "Diagnose the RocketMQ orders backlog using the operations manual, create one P1 ticket "
    "titled 'RocketMQ orders backlog', notify NOC, and request approval to restart consumer "
    "group orders-cg in the sandbox."
)
DIFFERENT_REQUEST = CANONICAL_REQUEST.replace("orders-cg", "billing-cg")


class WorkflowSnapshot(Protocol):
    workflow_id: str
    status: str
    approval_id: str | None


class DecisionSnapshot(Protocol):
    approval_id: str
    status: str
    version: int


class EffectCounts(Protocol):
    tickets: int
    notifications: int
    operations: int
    outbox_events: int


class AuditView(Protocol):
    action: str
    outcome: str
    reason_code: str | None


class FlagshipHarness(Protocol):
    async def submit(self, request: str, *, idempotency_key: str) -> WorkflowSnapshot: ...

    async def decide(
        self,
        approval_id: str,
        decision: str,
        *,
        idempotency_key: str,
    ) -> DecisionSnapshot: ...

    async def resume(self, workflow_id: str, *, command_id: str) -> WorkflowSnapshot: ...

    def effects(self, workflow_id: str) -> EffectCounts: ...

    def audit_events(self, workflow_id: str) -> tuple[AuditView, ...]: ...


def _new_harness() -> FlagshipHarness:
    """Load the same deterministic integration adapter used by T029."""

    try:
        module = import_module("knowflow.infrastructure.testing.flagship")
        factory = module.create_deterministic_flagship_harness
    except (AttributeError, ModuleNotFoundError):
        pytest.fail(
            "T030 requires create_deterministic_flagship_harness() in "
            "knowflow.infrastructure.testing.flagship; it must serialize concurrent resumes "
            "and expose committed effect/audit projections",
            pytrace=False,
        )
    return cast(FlagshipHarness, factory())


def _assert_single_business_effect(counts: EffectCounts) -> None:
    assert counts.tickets == 1
    assert counts.notifications == 1
    assert counts.operations == 1


async def _submit_until_approval(harness: FlagshipHarness, *, key: str) -> WorkflowSnapshot:
    snapshot = await harness.submit(CANONICAL_REQUEST, idempotency_key=key)
    assert snapshot.status == "WAITING_APPROVAL"
    assert snapshot.approval_id is not None
    return snapshot


async def test_same_key_and_payload_replays_but_payload_mismatch_conflicts_and_audits() -> None:
    harness = _new_harness()

    first = await harness.submit(CANONICAL_REQUEST, idempotency_key="workflow-replay-key")
    replay = await harness.submit(CANONICAL_REQUEST, idempotency_key="workflow-replay-key")

    assert replay.workflow_id == first.workflow_id
    assert replay.status == first.status
    assert harness.effects(first.workflow_id).tickets == 1
    assert harness.effects(first.workflow_id).notifications == 1

    with pytest.raises(KnowFlowError) as conflict_info:
        await harness.submit(DIFFERENT_REQUEST, idempotency_key="workflow-replay-key")

    assert conflict_info.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    conflicts = [
        event
        for event in harness.audit_events(first.workflow_id)
        if event.reason_code == "IDEMPOTENCY_PAYLOAD_MISMATCH"
    ]
    assert len(conflicts) == 1
    assert conflicts[0].action == "workflow.submit"
    assert conflicts[0].outcome == "CONFLICT"
    assert harness.effects(first.workflow_id).tickets == 1
    assert harness.effects(first.workflow_id).notifications == 1


@pytest.mark.parametrize("decision", ["APPROVED", "REJECTED"])
async def test_approval_decision_is_one_time_and_same_decision_replays(decision: str) -> None:
    harness = _new_harness()
    workflow = await _submit_until_approval(harness, key=f"approval-{decision.lower()}")
    assert workflow.approval_id is not None
    decision_key = f"decision-{decision.lower()}"

    first = await harness.decide(
        workflow.approval_id,
        decision,
        idempotency_key=decision_key,
    )
    replay = await harness.decide(
        workflow.approval_id,
        decision,
        idempotency_key=decision_key,
    )

    assert replay.approval_id == first.approval_id
    assert replay.status == first.status == decision
    assert replay.version == first.version

    opposite = "REJECTED" if decision == "APPROVED" else "APPROVED"
    with pytest.raises(KnowFlowError) as conflict_info:
        await harness.decide(
            workflow.approval_id,
            opposite,
            idempotency_key=f"decision-{opposite.lower()}",
        )

    assert conflict_info.value.code is ErrorCode.VERSION_CONFLICT


async def test_concurrent_resume_commands_converge_to_one_terminal_business_effect() -> None:
    harness = _new_harness()
    workflow = await _submit_until_approval(harness, key="concurrent-resume")
    assert workflow.approval_id is not None
    await harness.decide(
        workflow.approval_id,
        "APPROVED",
        idempotency_key="approve-concurrent-resume",
    )

    results = await asyncio.gather(
        *(harness.resume(workflow.workflow_id, command_id="approval-resume-v1") for _ in range(12))
    )

    assert {result.workflow_id for result in results} == {workflow.workflow_id}
    assert {result.status for result in results} == {"SUCCEEDED"}
    final_counts = harness.effects(workflow.workflow_id)
    _assert_single_business_effect(final_counts)
    assert final_counts.outbox_events >= 3


async def test_distinct_resume_command_ids_still_cannot_duplicate_the_bound_operation() -> None:
    harness = _new_harness()
    workflow = await _submit_until_approval(harness, key="distinct-resume-commands")
    assert workflow.approval_id is not None
    await harness.decide(
        workflow.approval_id,
        "APPROVED",
        idempotency_key="approve-distinct-resumes",
    )

    results = await asyncio.gather(
        *(
            harness.resume(workflow.workflow_id, command_id=f"resume-command-{index}")
            for index in range(8)
        )
    )

    assert all(result.status == "SUCCEEDED" for result in results)
    _assert_single_business_effect(harness.effects(workflow.workflow_id))
