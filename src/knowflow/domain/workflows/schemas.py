"""Strict domain schemas for workflow planning and clarification.

The module deliberately separates model-produced candidates from compiled domain
objects.  A model may propose intent, slot values, and graph edges, but it cannot
assert provenance, trust, authorization, durable task identity, or operation
identity.  Those fields exist only on the compiled schemas and must be supplied by
trusted application/compiler code.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SlotName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
]


class IntentType(StrEnum):
    """The six business intents supported by the MVP catalog."""

    KNOWLEDGE_QUERY = "KNOWLEDGE_QUERY"
    TICKET_CREATE = "TICKET_CREATE"
    TICKET_QUERY = "TICKET_QUERY"
    TICKET_UPDATE = "TICKET_UPDATE"
    NOTIFICATION_SEND = "NOTIFICATION_SEND"
    OPS_ACTION = "OPS_ACTION"


class RiskLevel(StrEnum):
    """Risk assigned by trusted policy, ordered from least to most sensitive."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class PlanStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    VALIDATED = "VALIDATED"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"


class DependencyKind(StrEnum):
    SEQUENCE = "SEQUENCE"
    DATA = "DATA"
    ON_SUCCESS = "ON_SUCCESS"
    ON_FAILURE = "ON_FAILURE"


class SlotDataType(StrEnum):
    STRING = "STRING"
    INTEGER = "INTEGER"
    NUMBER = "NUMBER"
    BOOLEAN = "BOOLEAN"
    UUID = "UUID"
    DATETIME = "DATETIME"
    STRING_LIST = "STRING_LIST"
    JSON = "JSON"


class SlotProvenance(StrEnum):
    """Trusted classification of where a compiled slot value originated."""

    USER_REQUEST = "USER_REQUEST"
    CLARIFICATION = "CLARIFICATION"
    TRUSTED_CONTEXT = "TRUSTED_CONTEXT"
    CATALOG_DEFAULT = "CATALOG_DEFAULT"
    TASK_OUTPUT = "TASK_OUTPUT"
    TOOL_RESULT = "TOOL_RESULT"
    MODEL_INFERENCE = "MODEL_INFERENCE"


class SlotTrustLevel(StrEnum):
    """How application code may use a slot; never accepted from model output."""

    UNTRUSTED = "UNTRUSTED"
    USER_ASSERTED = "USER_ASSERTED"
    VERIFIED = "VERIFIED"
    TRUSTED_CONTEXT = "TRUSTED_CONTEXT"


class ClarificationReason(StrEnum):
    MISSING_REQUIRED_SLOT = "MISSING_REQUIRED_SLOT"
    AMBIGUOUS_INTENT = "AMBIGUOUS_INTENT"
    AMBIGUOUS_SLOT = "AMBIGUOUS_SLOT"
    CONFLICTING_VALUES = "CONFLICTING_VALUES"
    UNAUTHORIZED_RESOURCE = "UNAUTHORIZED_RESOURCE"
    UNTRUSTED_IDENTIFIER = "UNTRUSTED_IDENTIFIER"


class ClarificationOutcome(StrEnum):
    PROCEED = "PROCEED"
    STILL_REQUIRED = "STILL_REQUIRED"
    REJECTED = "REJECTED"


class _StrictModel(BaseModel):
    """Immutable schema base that rejects undeclared model-controlled fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class UntrustedSlotCandidate(_StrictModel):
    """A slot proposal from a model or other untrusted request boundary.

    Provenance, trust level, authorization evidence, and operation identity are
    intentionally absent.  The deterministic compiler must attach those facts.
    """

    name: SlotName
    value_type: SlotDataType
    value: JsonValue
    source_span: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] = ""

    @model_validator(mode="after")
    def declared_type_matches_value(self) -> UntrustedSlotCandidate:
        _validate_slot_value(self.value_type, self.value)
        return self


class _IntentCandidateBase(_StrictModel):
    """Common, explicitly untrusted shape accepted from structured model output."""

    task_key: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=128,
            pattern=r"^[a-z][a-z0-9_-]*$",
        ),
    ]
    source_span: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]
    slots: tuple[UntrustedSlotCandidate, ...] = ()
    missing_slots: tuple[SlotName, ...] = ()
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def slot_names_are_unique(self) -> _IntentCandidateBase:
        names = [slot.name for slot in self.slots]
        if len(names) != len(set(names)):
            raise ValueError("intent candidate slot names must be unique")
        if set(names).intersection(self.missing_slots):
            raise ValueError("a slot cannot be both supplied and missing")
        if len(self.missing_slots) != len(set(self.missing_slots)):
            raise ValueError("missing slot names must be unique")
        return self


class KnowledgeQueryIntent(_IntentCandidateBase):
    intent: Literal[IntentType.KNOWLEDGE_QUERY] = IntentType.KNOWLEDGE_QUERY


class TicketCreateIntent(_IntentCandidateBase):
    intent: Literal[IntentType.TICKET_CREATE] = IntentType.TICKET_CREATE


class TicketQueryIntent(_IntentCandidateBase):
    intent: Literal[IntentType.TICKET_QUERY] = IntentType.TICKET_QUERY


class TicketUpdateIntent(_IntentCandidateBase):
    intent: Literal[IntentType.TICKET_UPDATE] = IntentType.TICKET_UPDATE


class NotificationSendIntent(_IntentCandidateBase):
    intent: Literal[IntentType.NOTIFICATION_SEND] = IntentType.NOTIFICATION_SEND


class OpsActionIntent(_IntentCandidateBase):
    intent: Literal[IntentType.OPS_ACTION] = IntentType.OPS_ACTION


type IntentCandidate = Annotated[
    KnowledgeQueryIntent
    | TicketCreateIntent
    | TicketQueryIntent
    | TicketUpdateIntent
    | NotificationSendIntent
    | OpsActionIntent,
    Field(discriminator="intent"),
]


class CandidateDependency(_StrictModel):
    """Untrusted graph edge expressed with local model task keys."""

    from_task_key: NonBlank
    to_task_key: NonBlank
    kind: DependencyKind
    output_binding: dict[SlotName, SlotName] | None = None

    @model_validator(mode="after")
    def reject_self_edge(self) -> CandidateDependency:
        if self.from_task_key == self.to_task_key:
            raise ValueError("a dependency cannot refer to the same task")
        return self


class ModelPlanCandidate(_StrictModel):
    """Versioned structured response model for an untrusted planner adapter."""

    schema_version: int = Field(ge=1)
    tasks: tuple[IntentCandidate, ...] = Field(min_length=1)
    dependencies: tuple[CandidateDependency, ...] = ()

    @model_validator(mode="after")
    def validate_candidate_graph_references(self) -> ModelPlanCandidate:
        task_keys = [task.task_key for task in self.tasks]
        if len(task_keys) != len(set(task_keys)):
            raise ValueError("candidate task keys must be unique")
        known = set(task_keys)
        edges: set[tuple[str, str, DependencyKind]] = set()
        for dependency in self.dependencies:
            if dependency.from_task_key not in known or dependency.to_task_key not in known:
                raise ValueError("candidate dependency refers to an unknown task")
            edge = (
                dependency.from_task_key,
                dependency.to_task_key,
                dependency.kind,
            )
            if edge in edges:
                raise ValueError("candidate dependencies must be unique")
            edges.add(edge)
        return self


class TypedSlot(_StrictModel):
    """A canonical slot after trusted resolution and provenance classification."""

    name: SlotName
    value_type: SlotDataType
    value: JsonValue
    provenance: SlotProvenance
    trust_level: SlotTrustLevel
    source_reference: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
    ] = None

    @model_validator(mode="after")
    def validate_value_and_trust_boundary(self) -> TypedSlot:
        _validate_slot_value(self.value_type, self.value)
        expected_trust = {
            SlotProvenance.USER_REQUEST: SlotTrustLevel.USER_ASSERTED,
            SlotProvenance.CLARIFICATION: SlotTrustLevel.USER_ASSERTED,
            SlotProvenance.TRUSTED_CONTEXT: SlotTrustLevel.TRUSTED_CONTEXT,
            SlotProvenance.CATALOG_DEFAULT: SlotTrustLevel.VERIFIED,
            SlotProvenance.TASK_OUTPUT: SlotTrustLevel.VERIFIED,
            SlotProvenance.TOOL_RESULT: SlotTrustLevel.VERIFIED,
            SlotProvenance.MODEL_INFERENCE: SlotTrustLevel.UNTRUSTED,
        }[self.provenance]
        if self.trust_level is not expected_trust:
            raise ValueError(f"{self.provenance.value} slots must use {expected_trust.value} trust")
        return self


class AtomicTask(_StrictModel):
    """One compiled task in a validated plan.

    ``operation_id`` remains optional here because only the capability-aware
    compiler knows whether a task can produce a side effect.  Before persistence
    or execution it must require exactly one stable operation ID for each
    side-effecting task and no operation ID for a pure read task.
    """

    id: UUID
    intent: IntentType
    source_span: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]
    slots: tuple[TypedSlot, ...] = ()
    missing_slots: tuple[SlotName, ...] = ()
    risk_level: RiskLevel
    side_effecting: bool
    operation_id: UUID | None = None
    position: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_slot_sets(self) -> AtomicTask:
        names = [slot.name for slot in self.slots]
        if len(names) != len(set(names)):
            raise ValueError("atomic task slot names must be unique")
        if set(names).intersection(self.missing_slots):
            raise ValueError("a slot cannot be both supplied and missing")
        if len(self.missing_slots) != len(set(self.missing_slots)):
            raise ValueError("missing slot names must be unique")
        return self


class PlanDependency(_StrictModel):
    from_task_id: UUID
    to_task_id: UUID
    kind: DependencyKind
    output_binding: dict[SlotName, SlotName] | None = None

    @model_validator(mode="after")
    def reject_self_edge(self) -> PlanDependency:
        if self.from_task_id == self.to_task_id:
            raise ValueError("a dependency cannot refer to the same task")
        return self


class StructuredPlan(_StrictModel):
    """A versioned, compiled plan safe for deterministic validation/persistence."""

    id: UUID
    workflow_id: UUID
    version: int = Field(ge=1)
    schema_version: int = Field(ge=1)
    source_model: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
    ]
    prompt_version: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
    ]
    normalized_request_hash: Annotated[
        str,
        StringConstraints(pattern=r"^[0-9a-f]{64}$"),
    ]
    status: PlanStatus
    risk_level: RiskLevel
    tasks: tuple[AtomicTask, ...] = Field(min_length=1)
    dependencies: tuple[PlanDependency, ...] = ()
    validation_summary: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_task_and_edge_identity(self) -> StructuredPlan:
        task_ids = [task.id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("plan task IDs must be unique")
        positions = [task.position for task in self.tasks]
        if len(positions) != len(set(positions)):
            raise ValueError("plan task positions must be unique")

        known = set(task_ids)
        edges: set[tuple[UUID, UUID, DependencyKind]] = set()
        for dependency in self.dependencies:
            if dependency.from_task_id not in known or dependency.to_task_id not in known:
                raise ValueError("plan dependency refers to an unknown task")
            edge = (
                dependency.from_task_id,
                dependency.to_task_id,
                dependency.kind,
            )
            if edge in edges:
                raise ValueError("plan dependencies must be unique")
            edges.add(edge)
        return self


class ClarificationRequest(_StrictModel):
    id: UUID
    workflow_id: UUID
    task_id: UUID | None = None
    expected_plan_version: int = Field(ge=1)
    reason: ClarificationReason
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ]
    slot_names: tuple[SlotName, ...] = Field(min_length=1)
    options: tuple[NonBlank, ...] = ()

    @field_validator("slot_names")
    @classmethod
    def slot_names_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("clarification slot names must be unique")
        return value


class ClarificationAnswer(_StrictModel):
    """Untrusted user input; the compiler must resolve it into ``TypedSlot`` values."""

    request_id: UUID
    expected_plan_version: int = Field(ge=1)
    answers: dict[SlotName, JsonValue] = Field(min_length=1)


class ClarificationResolution(_StrictModel):
    outcome: ClarificationOutcome
    resolved_slots: tuple[TypedSlot, ...] = ()
    follow_up: ClarificationRequest | None = None
    reason: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ] = None

    @model_validator(mode="after")
    def outcome_has_consistent_payload(self) -> ClarificationResolution:
        if self.outcome is ClarificationOutcome.STILL_REQUIRED and self.follow_up is None:
            raise ValueError("STILL_REQUIRED requires a follow-up clarification")
        if self.outcome is not ClarificationOutcome.STILL_REQUIRED and self.follow_up is not None:
            raise ValueError("only STILL_REQUIRED may include a follow-up clarification")
        if self.outcome is ClarificationOutcome.REJECTED and self.reason is None:
            raise ValueError("REJECTED requires a reason")
        return self


def _validate_slot_value(value_type: SlotDataType, value: JsonValue) -> None:
    """Validate JSON wire values without performing security-sensitive coercion."""

    matches = False
    if value_type is SlotDataType.STRING:
        matches = isinstance(value, str)
    elif value_type is SlotDataType.INTEGER:
        matches = isinstance(value, int) and not isinstance(value, bool)
    elif value_type is SlotDataType.NUMBER:
        matches = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif value_type is SlotDataType.BOOLEAN:
        matches = isinstance(value, bool)
    elif value_type is SlotDataType.UUID:
        if isinstance(value, str):
            try:
                UUID(value)
                matches = True
            except ValueError:
                matches = False
    elif value_type is SlotDataType.DATETIME:
        if isinstance(value, str):
            from datetime import datetime

            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                matches = parsed.tzinfo is not None
            except ValueError:
                matches = False
    elif value_type is SlotDataType.STRING_LIST:
        matches = isinstance(value, list) and all(isinstance(item, str) for item in value)
    elif value_type is SlotDataType.JSON:
        matches = True

    if not matches:
        raise ValueError(f"slot value does not match declared type {value_type.value}")


__all__ = [
    "AtomicTask",
    "CandidateDependency",
    "ClarificationAnswer",
    "ClarificationOutcome",
    "ClarificationReason",
    "ClarificationRequest",
    "ClarificationResolution",
    "DependencyKind",
    "IntentCandidate",
    "IntentType",
    "KnowledgeQueryIntent",
    "ModelPlanCandidate",
    "NotificationSendIntent",
    "OpsActionIntent",
    "PlanDependency",
    "PlanStatus",
    "RiskLevel",
    "SlotDataType",
    "SlotProvenance",
    "SlotTrustLevel",
    "StructuredPlan",
    "TicketCreateIntent",
    "TicketQueryIntent",
    "TicketUpdateIntent",
    "TypedSlot",
    "UntrustedSlotCandidate",
]
