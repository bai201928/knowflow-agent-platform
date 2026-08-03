"""Independent acceptance tests for T031, T032, and T035."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from typing import get_type_hints
from uuid import UUID, uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from knowflow.domain.workflows.catalog import (
    CAPABILITY_REGISTRY,
    INTENT_CATALOG,
    INTENT_CATALOG_VERSION,
    ApprovalPolicy,
    Capability,
    CapabilityDefinition,
    CatalogLookupError,
    IntentDefinition,
    SideEffect,
    SlotValidationError,
    get_capability_definition,
    get_intent_definition,
    get_tool_definition,
    missing_required_slots,
    require_permitted_role,
    resolve_capability_for_intent,
    role_is_permitted,
    validate_catalog,
    validate_slots,
)
from knowflow.domain.workflows.schemas import (
    AtomicTask,
    CandidateDependency,
    ClarificationOutcome,
    ClarificationReason,
    ClarificationRequest,
    ClarificationResolution,
    DependencyKind,
    IntentType,
    KnowledgeQueryIntent,
    ModelPlanCandidate,
    NotificationSendIntent,
    OpsActionIntent,
    PlanDependency,
    PlanStatus,
    RiskLevel,
    SlotDataType,
    SlotProvenance,
    SlotTrustLevel,
    StructuredPlan,
    TicketCreateIntent,
    TicketQueryIntent,
    TicketUpdateIntent,
    TypedSlot,
    UntrustedSlotCandidate,
)
from knowflow.infrastructure.db.models.identity import RoleCode
from knowflow.workflows.state import (
    MAX_STATE_SUMMARY_CHARS,
    SINGLE_VALUE_OWNERS,
    STATE_SCHEMA_VERSION,
    AuditRef,
    CommandRef,
    EvidenceRef,
    MessageRef,
    TaskResultRef,
    WorkflowState,
    initial_state,
    reduce_audit_refs,
    reduce_commands,
    reduce_evidence,
    reduce_messages,
    reduce_task_results,
)

INTENT_MODELS = (
    KnowledgeQueryIntent,
    TicketCreateIntent,
    TicketQueryIntent,
    TicketUpdateIntent,
    NotificationSendIntent,
    OpsActionIntent,
)


def _candidate_payload(intent: IntentType) -> dict[str, object]:
    return {
        "intent": intent,
        "task_key": "task_1",
        "source_span": "the user's bounded request",
        "slots": (),
        "missing_slots": (),
        "confidence": 0.8,
    }


def _task(*, task_id: UUID | None = None, position: int = 0) -> AtomicTask:
    return AtomicTask(
        id=task_id or uuid4(),
        intent=IntentType.KNOWLEDGE_QUERY,
        source_span="find the runbook",
        slots=(),
        missing_slots=(),
        risk_level=RiskLevel.LOW,
        side_effecting=False,
        position=position,
    )


def _plan(
    *tasks: AtomicTask,
    dependencies: tuple[PlanDependency, ...] = (),
    request_hash: str = "a" * 64,
) -> StructuredPlan:
    return StructuredPlan(
        id=uuid4(),
        workflow_id=uuid4(),
        version=1,
        schema_version=1,
        source_model="deterministic-stub",
        prompt_version="planner-v1",
        normalized_request_hash=request_hash,
        status=PlanStatus.VALIDATED,
        risk_level=RiskLevel.LOW,
        tasks=tasks or (_task(),),
        dependencies=dependencies,
    )


@pytest.mark.parametrize(
    ("model", "intent"),
    zip(INTENT_MODELS, tuple(IntentType), strict=True),
)
def test_all_six_intent_candidates_are_strict_and_discriminated(
    model: type[KnowledgeQueryIntent], intent: IntentType
) -> None:
    candidate = model.model_validate(_candidate_payload(intent))
    assert candidate.intent is intent
    assert candidate.model_config["frozen"] is True

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model.model_validate({**_candidate_payload(intent), "invented": "authority"})


@pytest.mark.parametrize(
    ("forged_field", "forged_value"),
    [
        ("provenance", "TRUSTED_CONTEXT"),
        ("trust_level", "VERIFIED"),
        ("operation_id", str(uuid4())),
        ("authorization_decision", "ALLOW"),
        ("permitted_roles", ["ADMIN"]),
        ("tool_key", "operations.restart_consumer"),
    ],
)
def test_model_candidates_cannot_forge_trusted_compiler_fields(
    forged_field: str, forged_value: object
) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        KnowledgeQueryIntent.model_validate(
            {**_candidate_payload(IntentType.KNOWLEDGE_QUERY), forged_field: forged_value}
        )


@pytest.mark.parametrize(
    ("value_type", "value"),
    [
        (SlotDataType.STRING, "text"),
        (SlotDataType.INTEGER, 4),
        (SlotDataType.NUMBER, 4.5),
        (SlotDataType.BOOLEAN, True),
        (SlotDataType.UUID, str(uuid4())),
        (SlotDataType.DATETIME, "2026-08-03T08:00:00+08:00"),
        (SlotDataType.STRING_LIST, ["a", "b"]),
        (SlotDataType.JSON, {"bounded": True}),
    ],
)
def test_untrusted_and_typed_slots_enforce_declared_wire_types(
    value_type: SlotDataType, value: object
) -> None:
    candidate = UntrustedSlotCandidate(name="target", value_type=value_type, value=value)
    assert candidate.value == value
    trusted = TypedSlot(
        name="target",
        value_type=value_type,
        value=value,
        provenance=SlotProvenance.CATALOG_DEFAULT,
        trust_level=SlotTrustLevel.VERIFIED,
    )
    assert trusted.value == value


@pytest.mark.parametrize(
    ("provenance", "trust"),
    [
        (SlotProvenance.USER_REQUEST, SlotTrustLevel.USER_ASSERTED),
        (SlotProvenance.CLARIFICATION, SlotTrustLevel.USER_ASSERTED),
        (SlotProvenance.TRUSTED_CONTEXT, SlotTrustLevel.TRUSTED_CONTEXT),
        (SlotProvenance.CATALOG_DEFAULT, SlotTrustLevel.VERIFIED),
        (SlotProvenance.TASK_OUTPUT, SlotTrustLevel.VERIFIED),
        (SlotProvenance.TOOL_RESULT, SlotTrustLevel.VERIFIED),
        (SlotProvenance.MODEL_INFERENCE, SlotTrustLevel.UNTRUSTED),
    ],
)
def test_typed_slot_provenance_has_exact_trust_mapping(
    provenance: SlotProvenance, trust: SlotTrustLevel
) -> None:
    slot = TypedSlot(
        name="target",
        value_type=SlotDataType.STRING,
        value="orders-cg",
        provenance=provenance,
        trust_level=trust,
    )
    assert slot.trust_level is trust

    wrong = next(level for level in SlotTrustLevel if level is not trust)
    with pytest.raises(ValidationError, match="slots must use"):
        TypedSlot(
            name="target",
            value_type=SlotDataType.STRING,
            value="orders-cg",
            provenance=provenance,
            trust_level=wrong,
        )


def test_candidate_and_structured_plan_identity_and_edges_are_closed() -> None:
    first = _task(position=0)
    second = _task(position=1)
    edge = PlanDependency(
        from_task_id=first.id,
        to_task_id=second.id,
        kind=DependencyKind.SEQUENCE,
    )
    plan = _plan(first, second, dependencies=(edge,))
    assert isinstance(plan.id, UUID)
    assert isinstance(plan.workflow_id, UUID)
    assert plan.dependencies == (edge,)

    with pytest.raises(ValidationError, match="task IDs must be unique"):
        _plan(first, _task(task_id=first.id, position=1))
    with pytest.raises(ValidationError, match="positions must be unique"):
        _plan(first, _task(position=0))
    with pytest.raises(ValidationError, match="unknown task"):
        _plan(
            first,
            dependencies=(
                PlanDependency(
                    from_task_id=first.id,
                    to_task_id=uuid4(),
                    kind=DependencyKind.DATA,
                ),
            ),
        )
    with pytest.raises(ValidationError, match="dependencies must be unique"):
        _plan(first, second, dependencies=(edge, edge))
    with pytest.raises(ValidationError, match="same task"):
        PlanDependency(
            from_task_id=first.id,
            to_task_id=first.id,
            kind=DependencyKind.SEQUENCE,
        )
    with pytest.raises(ValidationError):
        _plan(first, request_hash="not-a-sha256")
    with pytest.raises(ValidationError):
        StructuredPlan.model_validate(
            {
                **_plan(first).model_dump(mode="json"),
                "id": "not-a-uuid",
            }
        )


def test_model_plan_candidate_rejects_duplicate_keys_unknown_edges_and_self_edges() -> None:
    candidate = KnowledgeQueryIntent.model_validate(_candidate_payload(IntentType.KNOWLEDGE_QUERY))
    with pytest.raises(ValidationError, match="task keys must be unique"):
        ModelPlanCandidate(schema_version=1, tasks=(candidate, candidate))
    with pytest.raises(ValidationError, match="unknown task"):
        ModelPlanCandidate(
            schema_version=1,
            tasks=(candidate,),
            dependencies=(
                CandidateDependency(
                    from_task_key="task_1",
                    to_task_key="missing",
                    kind=DependencyKind.DATA,
                ),
            ),
        )
    with pytest.raises(ValidationError, match="same task"):
        CandidateDependency(
            from_task_key="task_1",
            to_task_key="task_1",
            kind=DependencyKind.SEQUENCE,
        )


def test_clarification_outcomes_are_payload_consistent_and_immutable() -> None:
    request = ClarificationRequest(
        id=uuid4(),
        workflow_id=uuid4(),
        expected_plan_version=1,
        reason=ClarificationReason.MISSING_REQUIRED_SLOT,
        question="Which severity should be used?",
        slot_names=("severity",),
        options=("P1", "P2"),
    )
    follow_up = ClarificationResolution(
        outcome=ClarificationOutcome.STILL_REQUIRED,
        follow_up=request,
    )
    assert follow_up.follow_up == request
    with pytest.raises(ValidationError, match="follow-up"):
        ClarificationResolution(outcome=ClarificationOutcome.STILL_REQUIRED)
    with pytest.raises(ValidationError, match="requires a reason"):
        ClarificationResolution(outcome=ClarificationOutcome.REJECTED)
    with pytest.raises(ValidationError, match="unique"):
        ClarificationRequest(**{**request.model_dump(), "slot_names": ("severity", "severity")})
    with pytest.raises(ValidationError):
        request.question = "mutated"


def test_catalog_is_complete_immutable_and_version_closed() -> None:
    validate_catalog()
    assert len(IntentType) == len(INTENT_CATALOG) == 6
    assert len(Capability) == len(CAPABILITY_REGISTRY) == 6
    assert set(INTENT_CATALOG) == set(IntentType)
    assert {item.catalog_version for item in INTENT_CATALOG.values()} == {INTENT_CATALOG_VERSION}
    with pytest.raises(TypeError):
        INTENT_CATALOG[IntentType.KNOWLEDGE_QUERY] = INTENT_CATALOG[IntentType.KNOWLEDGE_QUERY]
    with pytest.raises(FrozenInstanceError):
        INTENT_CATALOG[IntentType.KNOWLEDGE_QUERY].required_slots = frozenset()
    with pytest.raises(CatalogLookupError):
        get_intent_definition("MODEL_INVENTED_INTENT")
    with pytest.raises(CatalogLookupError):
        get_intent_definition(IntentType.KNOWLEDGE_QUERY, catalog_version="999")


@pytest.mark.parametrize("intent", tuple(IntentType))
def test_every_intent_resolves_one_allowlisted_capability_and_tool(intent: IntentType) -> None:
    intent_definition = get_intent_definition(intent)
    capability = resolve_capability_for_intent(intent)
    assert capability is get_capability_definition(intent_definition.capability)
    assert get_tool_definition(capability.tool_key) is capability
    assert capability.default_deadline_seconds > 0
    assert capability.permitted_roles
    assert capability.approval_policy is ApprovalPolicy.NEVER or capability.side_effect.is_write


def test_slot_role_and_tool_boundaries_fail_closed() -> None:
    definition = get_intent_definition(IntentType.TICKET_CREATE)
    valid = {slot: "value" for slot in definition.required_slots}
    validate_slots(IntentType.TICKET_CREATE, valid)
    assert missing_required_slots(IntentType.TICKET_CREATE, {}) == definition.required_slots
    with pytest.raises(SlotValidationError, match="missing required slots"):
        validate_slots(IntentType.TICKET_CREATE, {})
    with pytest.raises(SlotValidationError, match="unsupported slots"):
        validate_slots(IntentType.TICKET_CREATE, {**valid, "url": "https://evil.invalid"})
    with pytest.raises(CatalogLookupError, match="not allowlisted"):
        get_tool_definition("shell.exec")
    assert role_is_permitted(Capability.CONSUMER_RESTART, RoleCode.OPERATOR)
    assert not role_is_permitted(Capability.CONSUMER_RESTART, RoleCode.EMPLOYEE)
    with pytest.raises(PermissionError):
        require_permitted_role(Capability.CONSUMER_RESTART, RoleCode.EMPLOYEE)


@pytest.mark.parametrize(
    ("tool_key", "slots"),
    [
        ("remote.url.fetch", frozenset()),
        ("database.sql.query", frozenset()),
        ("system.shell.exec", frozenset()),
        ("safe.tool", frozenset({"url"})),
        ("safe.tool", frozenset({"sql"})),
        ("safe.tool", frozenset({"shell"})),
    ],
)
def test_catalog_construction_rejects_dynamic_url_sql_and_shell_inputs(
    tool_key: str, slots: frozenset[str]
) -> None:
    base_capability = next(iter(CAPABILITY_REGISTRY.values()))
    if slots:
        with pytest.raises(ValueError, match="dynamic execution inputs"):
            IntentDefinition(
                intent=IntentType.KNOWLEDGE_QUERY,
                catalog_version=INTENT_CATALOG_VERSION,
                capability=Capability.KNOWLEDGE_SEARCH,
                required_slots=slots,
                optional_slots=frozenset(),
            )
    else:
        with pytest.raises(ValueError, match="arbitrary execution primitives"):
            replace(base_capability, tool_key=tool_key)


def test_side_effect_approval_and_deadline_invariants_cannot_be_weakened() -> None:
    base = next(iter(CAPABILITY_REGISTRY.values()))
    with pytest.raises(ValueError, match="deadline"):
        replace(base, default_deadline_seconds=0)
    with pytest.raises(ValueError, match="read-only"):
        replace(base, side_effect=SideEffect.NONE, approval_policy=ApprovalPolicy.CONDITIONAL)
    with pytest.raises(ValueError, match="read-only"):
        CapabilityDefinition(
            key=base.key,
            tool_key=base.tool_key,
            permitted_roles=base.permitted_roles,
            side_effect=SideEffect.NONE,
            default_deadline_seconds=1,
            default_risk=base.default_risk,
            approval_policy=ApprovalPolicy.ALWAYS,
            output_contract=base.output_contract,
        )


def _message(ref_id: str, summary: str) -> MessageRef:
    return MessageRef(id=ref_id, role="user", summary=summary, payload_hash="a" * 64)


def _command(ref_id: str, *, version: int) -> CommandRef:
    return CommandRef(id=ref_id, kind="resume", payload_hash="b" * 64, accepted_version=version)


def _task_result(ref_id: str, summary: str) -> TaskResultRef:
    return TaskResultRef(
        id=ref_id,
        task_id=f"task-{ref_id}",
        status="SUCCEEDED",
        result_ref=f"result:{ref_id}",
        summary=summary,
        error_code=None,
    )


def _evidence(ref_id: str) -> EvidenceRef:
    return EvidenceRef(
        id=ref_id,
        task_id=f"task-{ref_id}",
        document_id="document-1",
        document_version_id="version-1",
        segment_id=f"segment-{ref_id}",
        content_hash="c" * 64,
        citation_label=f"citation-{ref_id}",
    )


def _audit(ref_id: str, sequence: int) -> AuditRef:
    return AuditRef(id=ref_id, sequence=sequence, event_type="workflow.updated")


@pytest.mark.parametrize(
    ("reducer", "left", "right", "updated_value"),
    [
        (
            reduce_messages,
            [_message("a", "old"), _message("b", "second")],
            [_message("a", "new"), _message("c", "third")],
            "new",
        ),
        (
            reduce_commands,
            [_command("a", version=1), _command("b", version=1)],
            [_command("a", version=2), _command("c", version=1)],
            2,
        ),
        (
            reduce_task_results,
            [_task_result("a", "old"), _task_result("b", "second")],
            [_task_result("a", "new"), _task_result("c", "third")],
            "new",
        ),
        (
            reduce_evidence,
            [_evidence("a"), _evidence("b")],
            [{**_evidence("a"), "citation_label": "new"}, _evidence("c")],
            "new",
        ),
        (
            reduce_audit_refs,
            [_audit("a", 1), _audit("b", 2)],
            [_audit("a", 9), _audit("c", 3)],
            9,
        ),
    ],
)
def test_all_reducers_are_pure_stable_and_last_value_wins(
    reducer: object,
    left: list[object],
    right: list[object],
    updated_value: object,
) -> None:
    before_left = deepcopy(left)
    before_right = deepcopy(right)
    result = reducer(left, right)  # type: ignore[operator]

    assert [item["id"] for item in result] == ["a", "b", "c"]
    assert left == before_left
    assert right == before_right
    assert result is not left and result is not right
    assert result[0] is not left[0] and result[0] is not right[0]
    assert updated_value in result[0].values()


@pytest.mark.parametrize(
    ("reducer", "valid"),
    [
        (reduce_messages, _message("ok", "summary")),
        (reduce_commands, _command("ok", version=1)),
        (reduce_task_results, _task_result("ok", "summary")),
        (reduce_evidence, _evidence("ok")),
        (reduce_audit_refs, _audit("ok", 1)),
    ],
)
def test_all_reducers_reject_missing_blank_and_non_string_ids(
    reducer: object, valid: dict[str, object]
) -> None:
    for illegal_id in (None, "", "   ", 123):
        malformed = {**valid, "id": illegal_id}
        with pytest.raises(TypeError, match="non-blank string id"):
            reducer([], [malformed])  # type: ignore[operator]


def test_summary_reducers_bound_and_normalize_checkpoint_text() -> None:
    long_multiline = (" sensitive\nbody\t" * 100) + "tail"
    message = reduce_messages([], [_message("m", long_multiline)])[0]
    result = reduce_task_results([], [_task_result("r", long_multiline)])[0]
    assert len(message["summary"]) == MAX_STATE_SUMMARY_CHARS
    assert len(result["summary"]) == MAX_STATE_SUMMARY_CHARS
    assert "\n" not in message["summary"] and "\t" not in message["summary"]
    assert "\n" not in result["summary"] and "\t" not in result["summary"]


def test_initial_state_has_version_owner_and_fresh_collections() -> None:
    first = initial_state(
        workflow_id="workflow-1",
        thread_id="thread-1",
        request_id="request-1",
        actor_user_id="user-1",
        workflow_version=1,
        deadline_at="2026-08-03T09:00:00+08:00",
    )
    second = initial_state(
        workflow_id="workflow-2",
        thread_id="thread-2",
        request_id="request-2",
        actor_user_id="user-2",
        workflow_version=7,
        deadline_at="2026-08-03T10:00:00+08:00",
    )
    assert first["schema_version"] == STATE_SCHEMA_VERSION == 1
    assert first["workflow_version"] == 1
    assert first["status"] == "ACCEPTED"
    assert set(SINGLE_VALUE_OWNERS) == {
        "workflow_id",
        "thread_id",
        "request_id",
        "actor_user_id",
        "schema_version",
        "workflow_version",
        "plan_id",
        "plan_version",
        "intent_name",
        "pending_clarification",
        "pending_approval",
        "deadline_at",
        "status",
        "error",
        "final_summary",
    }
    assert all(owner.strip() for owner in SINGLE_VALUE_OWNERS.values())
    first["messages"].append(_message("m", "summary"))
    assert second["messages"] == []


def test_workflow_state_is_langgraph_compatible_and_contains_only_safe_refs() -> None:
    graph = StateGraph(WorkflowState)

    def passthrough(state: WorkflowState) -> dict[str, object]:
        return {"status": state["status"]}

    graph.add_node("passthrough", passthrough)
    graph.add_edge(START, "passthrough")
    graph.add_edge("passthrough", END)
    compiled = graph.compile()
    state = initial_state(
        workflow_id="workflow-1",
        thread_id="thread-1",
        request_id="request-1",
        actor_user_id="user-1",
        workflow_version=1,
        deadline_at="2026-08-03T09:00:00+08:00",
    )
    assert compiled.invoke(state)["status"] == "ACCEPTED"

    state_fields = set(get_type_hints(WorkflowState, include_extras=True))
    nested_ref_fields = set().union(
        *(
            get_type_hints(ref_type)
            for ref_type in (MessageRef, CommandRef, TaskResultRef, EvidenceRef)
        )
    )
    forbidden = {"secret", "password", "token", "credential", "document_body", "body", "content"}
    assert state_fields.isdisjoint(forbidden)
    assert nested_ref_fields.isdisjoint(forbidden)
