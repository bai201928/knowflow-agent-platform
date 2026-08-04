"""T029: clarification is a hard write barrier for the flagship workflow.

The deterministic harness is an application-integration adapter: it composes the
real planner/workflow services with in-memory ports and performs no network or
container I/O.  Keeping that adapter outside this test makes the assertions
exercise production orchestration rather than a second implementation in tests.
"""

from __future__ import annotations

from importlib import import_module
from typing import Protocol, cast

import pytest


class ClarificationView(Protocol):
    code: str
    question: str


class WorkflowSnapshot(Protocol):
    workflow_id: str
    status: str
    clarification: ClarificationView | None


class EffectCounts(Protocol):
    tickets: int
    notifications: int
    operations: int
    outbox_events: int


class FlagshipHarness(Protocol):
    async def submit(self, request: str, *, idempotency_key: str) -> WorkflowSnapshot: ...

    async def clarify(
        self,
        workflow_id: str,
        answer: str,
        *,
        idempotency_key: str,
    ) -> WorkflowSnapshot: ...

    def effects(self, workflow_id: str) -> EffectCounts: ...


def _new_harness() -> FlagshipHarness:
    """Load the offline integration adapter with an actionable test-first failure."""

    try:
        module = import_module("knowflow.infrastructure.testing.flagship")
        factory = module.create_deterministic_flagship_harness
    except (AttributeError, ModuleNotFoundError):
        pytest.fail(
            "T029 requires create_deterministic_flagship_harness() in "
            "knowflow.infrastructure.testing.flagship; it must compose real application "
            "services with deterministic in-memory ports",
            pytrace=False,
        )
    return cast(FlagshipHarness, factory())


def _assert_no_write_effects(counts: EffectCounts) -> None:
    assert counts.tickets == 0
    assert counts.notifications == 0
    assert counts.operations == 0
    assert counts.outbox_events == 0


@pytest.mark.parametrize(
    ("workflow_prompt", "expected_code", "answer"),
    [
        pytest.param(
            (
                "Diagnose the RocketMQ backlog, create a ticket, notify NOC, and request "
                "approval to restart consumer group orders-cg."
            ),
            "MISSING_CRITICAL_SLOT",
            "Use severity P1 and title 'RocketMQ orders backlog'.",
            id="missing-ticket-severity-and-title",
        ),
        pytest.param(
            (
                "Diagnose the RocketMQ backlog, create a P1 ticket titled 'Orders backlog', "
                "notify NOC, then either restart or scale consumer group orders-cg."
            ),
            "HIGH_IMPACT_AMBIGUITY",
            "Choose restart only; do not scale the consumer group.",
            id="ambiguous-sensitive-operation",
        ),
    ],
)
async def test_open_clarification_blocks_every_write_effect(
    workflow_prompt: str,
    expected_code: str,
    answer: str,
) -> None:
    harness = _new_harness()

    pending = await harness.submit(workflow_prompt, idempotency_key=f"clarify-{expected_code}")

    assert pending.status == "CLARIFYING"
    assert pending.clarification is not None
    assert pending.clarification.code == expected_code
    assert pending.clarification.question.strip()
    _assert_no_write_effects(harness.effects(pending.workflow_id))

    progressed = await harness.clarify(
        pending.workflow_id,
        answer,
        idempotency_key=f"answer-{expected_code}",
    )
    counts_after_answer = harness.effects(pending.workflow_id)

    assert progressed.workflow_id == pending.workflow_id
    assert progressed.status in {"PLANNED", "RUNNING", "WAITING_APPROVAL", "SUCCEEDED"}
    assert progressed.clarification is None
    assert counts_after_answer.tickets == 1
    assert counts_after_answer.notifications == 1
    assert counts_after_answer.operations >= 0
    assert counts_after_answer.outbox_events >= 2


async def test_invalid_clarification_answer_keeps_the_write_barrier_closed() -> None:
    harness = _new_harness()
    pending = await harness.submit(
        "Diagnose RocketMQ backlog and create a ticket, then notify NOC.",
        idempotency_key="clarify-invalid-answer",
    )

    still_pending = await harness.clarify(
        pending.workflow_id,
        "Whatever you think is best.",
        idempotency_key="answer-invalid",
    )

    assert still_pending.status == "CLARIFYING"
    assert still_pending.clarification is not None
    _assert_no_write_effects(harness.effects(pending.workflow_id))
