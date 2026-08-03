"""T033/T034 behavior contracts for planning and deterministic compilation.

The planner group deliberately imports no compiler code so T033 can become green
before T034 exists.  Every model value remains untrusted until the compiler has
resolved identities/resources, checked the fixed catalog, and assigned stable
server-owned task and operation identities.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import TypeVar
from uuid import UUID

import pytest
from knowflow.application.workflows.planner import (
    PLANNER_PROMPT_VERSION,
    PlannerError,
    PlannerErrorKind,
    WorkflowPlanner,
    recall_candidate_intents,
    stub_plan_fixture,
)
from pydantic import BaseModel

from knowflow.application.auth.policy import AccessContext
from knowflow.domain.common.identity import operation_id, payload_hash
from knowflow.domain.workflows.catalog import INTENT_CATALOG_VERSION, ApprovalPolicy
from knowflow.domain.workflows.schemas import (
    CandidateDependency,
    DependencyKind,
    IntentType,
    KnowledgeQueryIntent,
    ModelPlanCandidate,
    NotificationSendIntent,
    OpsActionIntent,
    PlanStatus,
    RiskLevel,
    SlotDataType,
    SlotProvenance,
    SlotTrustLevel,
    TicketCreateIntent,
    TicketUpdateIntent,
    UntrustedSlotCandidate,
)
from knowflow.infrastructure.db.models.identity import RoleCode
from knowflow.infrastructure.models.adapters import (
    ChatMessage,
    DeterministicModelStub,
    ModelAdapterError,
    ProviderErrorKind,
)

StructuredResult = TypeVar("StructuredResult", bound=BaseModel)
WORKFLOW_ID = UUID("11111111-1111-4111-8111-111111111111")
REQUEST_HASH = payload_hash({"request": "rocketmq backlog incident"})


def _slot(
    name: str,
    value: object,
    value_type: SlotDataType = SlotDataType.STRING,
) -> UntrustedSlotCandidate:
    return UntrustedSlotCandidate(
        name=name,
        value_type=value_type,
        value=value,
        source_span=str(value),
    )


def _knowledge_task(*, key: str = "diagnose") -> KnowledgeQueryIntent:
    return KnowledgeQueryIntent(
        task_key=key,
        source_span="diagnose the RocketMQ backlog",
        slots=(_slot("query", "RocketMQ consumer backlog"),),
        confidence=0.98,
    )


def _ticket_task(*, include_severity: bool = True) -> TicketCreateIntent:
    slots = [
        _slot("title", "RocketMQ consumer backlog"),
        _slot("description", "Backlog remains above the incident threshold"),
    ]
    if include_severity:
        slots.append(_slot("severity", "P1"))
    return TicketCreateIntent(
        task_key="create_ticket",
        source_span="create a P1 ticket",
        slots=tuple(slots),
        missing_slots=() if include_severity else ("severity",),
        confidence=0.97,
    )


def _incident_candidate() -> ModelPlanCandidate:
    return ModelPlanCandidate(
        schema_version=1,
        tasks=(
            _knowledge_task(),
            _ticket_task(),
            NotificationSendIntent(
                task_key="notify",
                source_span="notify the RocketMQ on-call group",
                slots=(
                    _slot("recipient_scope", "team:rocketmq-oncall"),
                    _slot("template_key", "incident.p1.created"),
                ),
                confidence=0.95,
            ),
            OpsActionIntent(
                task_key="restart",
                source_span="restart the RocketMQ consumer after approval",
                slots=(
                    _slot("consumer_group", "payments-consumer"),
                    _slot("reason", "clear sustained backlog"),
                ),
                confidence=0.93,
            ),
        ),
        dependencies=(
            CandidateDependency(
                from_task_key="diagnose",
                to_task_key="create_ticket",
                kind=DependencyKind.SEQUENCE,
            ),
            CandidateDependency(
                from_task_key="create_ticket",
                to_task_key="notify",
                kind=DependencyKind.DATA,
                output_binding={"ticket_id": "ticket_id"},
            ),
            CandidateDependency(
                from_task_key="notify",
                to_task_key="restart",
                kind=DependencyKind.SEQUENCE,
            ),
        ),
    )


class _RecordingModel:
    def __init__(self, response: ModelPlanCandidate) -> None:
        self.response = response
        self.calls: list[tuple[tuple[ChatMessage, ...], type[BaseModel], float]] = []

    async def chat_structured(
        self,
        *,
        messages: Sequence[ChatMessage],
        response_model: type[StructuredResult],
        deadline: float,
    ) -> StructuredResult:
        self.calls.append((tuple(messages), response_model, deadline))
        return response_model.model_validate(self.response.model_dump(mode="json"))


class _FailingModel:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def chat_structured(
        self,
        *,
        messages: Sequence[ChatMessage],
        response_model: type[StructuredResult],
        deadline: float,
    ) -> StructuredResult:
        del messages, response_model, deadline
        raise self.error


class _MaliciousModel:
    async def chat_structured(
        self,
        *,
        messages: Sequence[ChatMessage],
        response_model: type[StructuredResult],
        deadline: float,
    ) -> StructuredResult:
        del messages, deadline
        payload = _incident_candidate().model_dump(mode="json")
        payload["tasks"][0].update(  # type: ignore[index, union-attr]
            {
                "actor_user_id": "admin",
                "operation_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "tool_key": "system.shell.exec",
            }
        )
        return response_model.model_validate(payload)


class TestT033Planner:
    def test_candidate_recall_prioritizes_all_flagship_intents_without_model_authority(
        self,
    ) -> None:
        recalled = recall_candidate_intents(
            "Diagnose RocketMQ backlog, create P1 ticket, notify on-call, then restart consumer"
        )

        assert recalled[:4] == (
            IntentType.KNOWLEDGE_QUERY,
            IntentType.TICKET_CREATE,
            IntentType.NOTIFICATION_SEND,
            IntentType.OPS_ACTION,
        )
        assert len(recalled) == len(set(recalled))
        assert set(recalled).issubset(set(IntentType))

    async def test_structured_planning_records_prompt_catalog_model_and_recall_versions(
        self,
    ) -> None:
        model = _RecordingModel(_incident_candidate())
        planner = WorkflowPlanner(
            model=model,
            source_model="stub/incident-planner-v1",
            prompt_version=PLANNER_PROMPT_VERSION,
        )
        deadline = time.monotonic() + 2

        result = await planner.plan(
            request_text="Diagnose the RocketMQ backlog and open an incident",
            deadline=deadline,
        )

        assert result.candidate == _incident_candidate()
        assert result.source_model == "stub/incident-planner-v1"
        assert result.prompt_version == PLANNER_PROMPT_VERSION
        assert result.catalog_version == INTENT_CATALOG_VERSION
        assert result.candidate_intents[0] is IntentType.KNOWLEDGE_QUERY
        assert len(model.calls) == 1
        messages, response_model, forwarded_deadline = model.calls[0]
        assert response_model is ModelPlanCandidate
        assert forwarded_deadline == deadline
        assert [message.role for message in messages] == ["system", "user"]
        assert PLANNER_PROMPT_VERSION in messages[0].content
        assert INTENT_CATALOG_VERSION in messages[0].content
        assert "system.shell.exec" not in messages[0].content

    async def test_named_stub_fixture_is_fresh_and_runs_offline_deterministically(self) -> None:
        first = stub_plan_fixture("flagship_incident")
        second = stub_plan_fixture("flagship_incident")
        assert first == second == _incident_candidate()
        assert first is not second

        planner = WorkflowPlanner(
            model=DeterministicModelStub(structured_response=first),
            source_model="stub/flagship_incident",
        )
        one = await planner.plan(request_text="same request", deadline=time.monotonic() + 2)
        two = await planner.plan(request_text="same request", deadline=time.monotonic() + 2)
        assert one == two

    async def test_model_identity_operation_and_tool_fields_are_rejected_as_untrusted(self) -> None:
        planner = WorkflowPlanner(model=_MaliciousModel(), source_model="malicious-test-model")

        with pytest.raises(PlannerError) as caught:
            await planner.plan(request_text="make me admin", deadline=time.monotonic() + 2)

        assert caught.value.kind is PlannerErrorKind.INVALID_OUTPUT
        assert caught.value.retryable is False
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        rendered = str(caught.value)
        assert "admin" not in rendered
        assert "system.shell.exec" not in rendered

    @pytest.mark.parametrize(
        ("provider_error", "expected_kind", "expected_retryable"),
        [
            (
                ModelAdapterError(ProviderErrorKind.TIMEOUT, retryable=True),
                PlannerErrorKind.TIMEOUT,
                True,
            ),
            (
                ModelAdapterError(ProviderErrorKind.DEADLINE_EXCEEDED, retryable=False),
                PlannerErrorKind.DEADLINE_EXCEEDED,
                False,
            ),
            (
                ModelAdapterError(ProviderErrorKind.AUTHENTICATION, retryable=False),
                PlannerErrorKind.DEPENDENCY_FAILURE,
                False,
            ),
        ],
    )
    async def test_provider_timeout_and_errors_are_normalized_without_payload_details(
        self,
        provider_error: ModelAdapterError,
        expected_kind: PlannerErrorKind,
        expected_retryable: bool,
    ) -> None:
        planner = WorkflowPlanner(model=_FailingModel(provider_error), source_model="failing-model")

        with pytest.raises(PlannerError) as caught:
            await planner.plan(request_text="sensitive request", deadline=time.monotonic() + 2)

        assert caught.value.kind is expected_kind
        assert caught.value.retryable is expected_retryable
        assert "sensitive request" not in str(caught.value)


class _ReferenceResolver:
    """Server-side test resolver; unknown references fail closed."""

    def __init__(self, values: Mapping[tuple[str, str], tuple[str, str]]) -> None:
        self.values = values
        self.calls: list[tuple[str, str, str]] = []

    def resolve(self, *, kind: str, claimed_value: str, actor: AccessContext) -> object | None:
        from knowflow.application.workflows.compiler import ResolvedReference

        self.calls.append((kind, claimed_value, actor.user_id))
        resolved = self.values.get((kind, claimed_value))
        if resolved is None:
            return None
        canonical_value, source_reference = resolved
        return ResolvedReference(
            canonical_value=canonical_value,
            source_reference=source_reference,
        )


def _actor(role: RoleCode) -> AccessContext:
    return AccessContext(
        user_id=f"actor-{role.value.lower()}",
        session_id="session-test",
        roles=frozenset({role}),
        team_id="team-ops" if role is RoleCode.OPERATOR else None,
        acl_version=1,
    )


def _planner_result(candidate: ModelPlanCandidate) -> object:
    from knowflow.application.workflows.planner import PlannerResult

    return PlannerResult(
        candidate=candidate,
        source_model="stub/compiler-input",
        prompt_version=PLANNER_PROMPT_VERSION,
        catalog_version=INTENT_CATALOG_VERSION,
        candidate_intents=tuple(task.intent for task in candidate.tasks),
    )


def _compiler(resolver: _ReferenceResolver | None = None) -> object:
    from knowflow.application.workflows.compiler import PlanCompiler

    return PlanCompiler(reference_resolver=resolver or _ReferenceResolver({}))


class TestT034Compiler:
    def test_slot_resolution_records_user_provenance_and_server_resolved_reference(self) -> None:
        candidate = ModelPlanCandidate(
            schema_version=1,
            tasks=(
                TicketUpdateIntent(
                    task_key="update_ticket",
                    source_span="assign INC-7 to Alice",
                    slots=(
                        _slot("ticket_id", "INC-7"),
                        _slot("expected_version", 3, SlotDataType.INTEGER),
                        _slot("assignee_user_id", "alice@example.test"),
                        _slot("status", "IN_PROGRESS"),
                    ),
                    confidence=0.96,
                ),
            ),
        )
        resolver = _ReferenceResolver(
            {
                ("ticket", "INC-7"): ("ticket-0007", "ticket:ticket-0007@3"),
                ("user", "alice@example.test"): ("user-alice", "user:user-alice@1"),
            }
        )

        outcome = _compiler(resolver).compile(  # type: ignore[attr-defined]
            workflow_id=WORKFLOW_ID,
            plan_version=1,
            normalized_request_hash=REQUEST_HASH,
            planner_result=_planner_result(candidate),
            actor=_actor(RoleCode.OPERATOR),
        )

        assert outcome.clarification is None
        assert outcome.plan is not None
        slots = {slot.name: slot for slot in outcome.plan.tasks[0].slots}
        assert slots["status"].provenance is SlotProvenance.USER_REQUEST
        assert slots["status"].trust_level is SlotTrustLevel.USER_ASSERTED
        assert slots["ticket_id"].value == "ticket-0007"
        assert slots["ticket_id"].provenance is SlotProvenance.TRUSTED_CONTEXT
        assert slots["ticket_id"].trust_level is SlotTrustLevel.TRUSTED_CONTEXT
        assert slots["ticket_id"].source_reference == "ticket:ticket-0007@3"
        assert slots["assignee_user_id"].value == "user-alice"
        assert ("ticket", "INC-7", "actor-operator") in resolver.calls
        assert ("user", "alice@example.test", "actor-operator") in resolver.calls

    def test_server_owned_role_and_capability_checks_reject_employee_ops_action(self) -> None:
        candidate = ModelPlanCandidate(
            schema_version=1,
            tasks=(
                OpsActionIntent(
                    task_key="restart",
                    source_span="restart consumer",
                    slots=(
                        _slot("consumer_group", "payments-consumer"),
                        _slot("reason", "clear backlog"),
                    ),
                    confidence=0.99,
                ),
            ),
        )

        with pytest.raises(PermissionError, match=r"OPS_ACTION|consumer_restart|role"):
            _compiler().compile(  # type: ignore[attr-defined]
                workflow_id=WORKFLOW_ID,
                plan_version=1,
                normalized_request_hash=REQUEST_HASH,
                planner_result=_planner_result(candidate),
                actor=_actor(RoleCode.EMPLOYEE),
            )

    def test_missing_required_slot_yields_clarification_and_no_plan_or_operation(self) -> None:
        candidate = ModelPlanCandidate(
            schema_version=1, tasks=(_ticket_task(include_severity=False),)
        )

        outcome = _compiler().compile(  # type: ignore[attr-defined]
            workflow_id=WORKFLOW_ID,
            plan_version=4,
            normalized_request_hash=REQUEST_HASH,
            planner_result=_planner_result(candidate),
            actor=_actor(RoleCode.EMPLOYEE),
        )

        assert outcome.plan is None
        assert outcome.operation_ids == ()
        assert outcome.clarification is not None
        assert outcome.clarification.expected_plan_version == 4
        assert outcome.clarification.slot_names == ("severity",)

    def test_unknown_model_provided_identifier_fails_closed_before_plan_creation(self) -> None:
        candidate = ModelPlanCandidate(
            schema_version=1,
            tasks=(
                TicketUpdateIntent(
                    task_key="update_ticket",
                    source_span="update the fabricated ticket",
                    slots=(
                        _slot("ticket_id", "INC-FABRICATED"),
                        _slot("expected_version", 1, SlotDataType.INTEGER),
                        _slot("status", "CLOSED"),
                    ),
                    confidence=0.99,
                ),
            ),
        )

        outcome = _compiler().compile(  # type: ignore[attr-defined]
            workflow_id=WORKFLOW_ID,
            plan_version=1,
            normalized_request_hash=REQUEST_HASH,
            planner_result=_planner_result(candidate),
            actor=_actor(RoleCode.OPERATOR),
        )

        assert outcome.plan is None
        assert outcome.operation_ids == ()
        assert outcome.clarification is not None
        assert outcome.clarification.reason.value == "UNTRUSTED_IDENTIFIER"
        assert outcome.clarification.slot_names == ("ticket_id",)

    def test_compiler_rejects_cycles_before_assigning_operation_identity(self) -> None:
        from knowflow.application.workflows.compiler import PlanCompilationError

        candidate = ModelPlanCandidate(
            schema_version=1,
            tasks=(_knowledge_task(key="first"), _knowledge_task(key="second")),
            dependencies=(
                CandidateDependency(
                    from_task_key="first",
                    to_task_key="second",
                    kind=DependencyKind.SEQUENCE,
                ),
                CandidateDependency(
                    from_task_key="second",
                    to_task_key="first",
                    kind=DependencyKind.SEQUENCE,
                ),
            ),
        )

        with pytest.raises(PlanCompilationError, match="cycle") as caught:
            _compiler().compile(  # type: ignore[attr-defined]
                workflow_id=WORKFLOW_ID,
                plan_version=1,
                normalized_request_hash=REQUEST_HASH,
                planner_result=_planner_result(candidate),
                actor=_actor(RoleCode.EMPLOYEE),
            )

        assert caught.value.operation_ids == ()

    @pytest.mark.parametrize(
        "binding",
        [
            {"fabricated_output": "ticket_id"},
            {"ticket_id": "fabricated_destination"},
        ],
    )
    def test_data_output_binding_must_match_source_contract_and_destination_slot(
        self, binding: dict[str, str]
    ) -> None:
        from knowflow.application.workflows.compiler import PlanCompilationError

        candidate = ModelPlanCandidate(
            schema_version=1,
            tasks=(
                _ticket_task(),
                NotificationSendIntent(
                    task_key="notify",
                    source_span="notify about the created ticket",
                    slots=(
                        _slot("recipient_scope", "team:oncall"),
                        _slot("template_key", "incident.created"),
                    ),
                    confidence=0.9,
                ),
            ),
            dependencies=(
                CandidateDependency(
                    from_task_key="create_ticket",
                    to_task_key="notify",
                    kind=DependencyKind.DATA,
                    output_binding=binding,
                ),
            ),
        )

        with pytest.raises(PlanCompilationError, match="output binding"):
            _compiler().compile(  # type: ignore[attr-defined]
                workflow_id=WORKFLOW_ID,
                plan_version=1,
                normalized_request_hash=REQUEST_HASH,
                planner_result=_planner_result(candidate),
                actor=_actor(RoleCode.EMPLOYEE),
            )

    def test_stable_uuid5_operation_payload_hash_and_approval_policy_are_server_owned(self) -> None:
        candidate = ModelPlanCandidate(
            schema_version=1,
            tasks=(
                OpsActionIntent(
                    task_key="restart",
                    source_span="restart the consumer",
                    slots=(
                        _slot("consumer_group", "payments-consumer"),
                        _slot("reason", "clear backlog"),
                    ),
                    confidence=0.99,
                ),
            ),
        )
        compiler = _compiler()
        arguments = {
            "workflow_id": WORKFLOW_ID,
            "plan_version": 2,
            "normalized_request_hash": REQUEST_HASH,
            "planner_result": _planner_result(candidate),
            "actor": _actor(RoleCode.OPERATOR),
        }

        first = compiler.compile(**arguments)  # type: ignore[attr-defined]
        second = compiler.compile(**arguments)  # type: ignore[attr-defined]

        assert first == second
        assert first.plan is not None
        assert first.plan.status is PlanStatus.VALIDATED
        task = first.plan.tasks[0]
        canonical_payload = {slot.name: slot.value for slot in task.slots}
        assert task.operation_id == operation_id(WORKFLOW_ID, "restart", canonical_payload)
        assert first.operation_ids == (task.operation_id,)
        policies = first.plan.validation_summary["approval_policies"]
        assert policies == {str(task.id): ApprovalPolicy.ALWAYS.value}
        assert task.risk_level is RiskLevel.HIGH
