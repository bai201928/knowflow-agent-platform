"""Deterministic, server-authoritative compilation of untrusted workflow plans.

The planner may suggest intents, local task keys, slot values, and graph edges.  This
module is the trust boundary that resolves references, applies the immutable catalog,
checks authorization, validates the complete graph, and only then allocates stable
identities for side effects.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID, uuid5

from pydantic import JsonValue

from knowflow.application.auth.policy import AccessContext
from knowflow.application.workflows.planner import PlannerResult
from knowflow.domain.common.identity import operation_id, payload_hash
from knowflow.domain.workflows.catalog import (
    INTENT_CATALOG_VERSION,
    CapabilityDefinition,
    IntentDefinition,
    get_intent_definition,
    resolve_capability_for_intent,
)
from knowflow.domain.workflows.schemas import (
    AtomicTask,
    CandidateDependency,
    ClarificationReason,
    ClarificationRequest,
    DependencyKind,
    IntentCandidate,
    IntentType,
    ModelPlanCandidate,
    PlanDependency,
    PlanStatus,
    RiskLevel,
    SlotProvenance,
    SlotTrustLevel,
    StructuredPlan,
    TypedSlot,
    UntrustedSlotCandidate,
)


@dataclass(frozen=True, slots=True)
class ResolvedReference:
    """Canonical resource identity returned by a trusted server-side resolver."""

    canonical_value: str
    source_reference: str

    def __post_init__(self) -> None:
        if not self.canonical_value.strip() or not self.source_reference.strip():
            raise ValueError("resolved references must be non-blank")


class ReferenceResolver(Protocol):
    """Resolve and authorize a model-provided resource reference for one actor."""

    def resolve(
        self,
        *,
        kind: str,
        claimed_value: str,
        actor: AccessContext,
    ) -> object | None: ...


@dataclass(frozen=True, slots=True)
class CompilationOutcome:
    """Exactly one validated plan or clarification request, never both."""

    plan: StructuredPlan | None
    clarification: ClarificationRequest | None
    operation_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if (self.plan is None) == (self.clarification is None):
            raise ValueError("compilation must produce exactly one outcome")
        if self.plan is None and self.operation_ids:
            raise ValueError("clarification outcomes cannot allocate operation identities")


class PlanCompilationError(ValueError):
    """A deterministic validation failure raised before side-effect allocation."""

    def __init__(self, message: str, *, operation_ids: tuple[UUID, ...] = ()) -> None:
        self.operation_ids = operation_ids
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _ValidatedTask:
    candidate: IntentCandidate
    intent: IntentDefinition
    capability: CapabilityDefinition
    slots: tuple[TypedSlot, ...]


@dataclass(frozen=True, slots=True)
class _ClarificationNeeded:
    task_key: str
    slot_names: tuple[str, ...]
    reason: ClarificationReason


_REFERENCE_KINDS: Mapping[str, str] = {
    "ticket_id": "ticket",
    "assignee_user_id": "user",
    "assigned_team_id": "team",
    "workflow_id": "workflow",
}
_LIST_REFERENCE_KINDS: Mapping[str, str] = {"collection_ids": "collection"}
_RISK_ORDER: Mapping[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


class PlanCompiler:
    """Compile an untrusted planner result into one stable validated plan."""

    def __init__(self, *, reference_resolver: ReferenceResolver) -> None:
        self._reference_resolver = reference_resolver

    def compile(
        self,
        *,
        workflow_id: UUID,
        plan_version: int,
        normalized_request_hash: str,
        planner_result: PlannerResult,
        actor: AccessContext,
    ) -> CompilationOutcome:
        """Validate all authority and graph facts before allocating operations."""

        if plan_version < 1:
            raise PlanCompilationError("plan version must be positive")
        if planner_result.catalog_version != INTENT_CATALOG_VERSION:
            raise PlanCompilationError("planner result uses an unsupported catalog version")
        if len(normalized_request_hash) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_request_hash
        ):
            raise PlanCompilationError("normalized request hash must be a lowercase SHA-256 value")

        candidate = planner_result.candidate
        self._validate_graph(candidate)

        validated: list[_ValidatedTask] = []
        clarification: _ClarificationNeeded | None = None
        for task in candidate.tasks:
            intent = get_intent_definition(task.intent)
            capability = resolve_capability_for_intent(task.intent)
            self._require_actor_role(task.intent.value, capability, actor)
            self._validate_declared_slots(task.task_key, intent, task.slots, task.missing_slots)

            required_missing = intent.required_slots.difference(slot.name for slot in task.slots)
            if required_missing:
                clarification = _ClarificationNeeded(
                    task_key=task.task_key,
                    slot_names=tuple(sorted(required_missing)),
                    reason=ClarificationReason.MISSING_REQUIRED_SLOT,
                )
                break

            resolved_slots, resolution_failure = self._resolve_slots(
                task_key=task.task_key,
                slots=task.slots,
                actor=actor,
            )
            if resolution_failure is not None:
                clarification = resolution_failure
                break
            validated.append(
                _ValidatedTask(
                    candidate=task,
                    intent=intent,
                    capability=capability,
                    slots=resolved_slots,
                )
            )

        if clarification is not None:
            request = self._clarification_request(
                workflow_id=workflow_id,
                plan_version=plan_version,
                normalized_request_hash=normalized_request_hash,
                need=clarification,
            )
            return CompilationOutcome(plan=None, clarification=request)

        # Every fallible catalog, authorization, slot, reference, and graph check is
        # complete at this point.  Side-effect operation identity allocation is last.
        return self._build_plan(
            workflow_id=workflow_id,
            plan_version=plan_version,
            normalized_request_hash=normalized_request_hash,
            planner_result=planner_result,
            tasks=tuple(validated),
        )

    @staticmethod
    def _require_actor_role(
        intent_name: str,
        capability: CapabilityDefinition,
        actor: AccessContext,
    ) -> None:
        if actor.roles.isdisjoint(capability.permitted_roles):
            roles = ",".join(sorted(role.value for role in actor.roles))
            raise PermissionError(
                f"role {roles} cannot use capability {capability.key.value} "
                f"for intent {intent_name}"
            )

    @staticmethod
    def _validate_declared_slots(
        task_key: str,
        intent: IntentDefinition,
        slots: tuple[UntrustedSlotCandidate, ...],
        missing_slots: tuple[str, ...],
    ) -> None:
        supplied = {slot.name for slot in slots}
        declared_missing = set(missing_slots)
        unknown = supplied.union(declared_missing).difference(intent.allowed_slots)
        if unknown:
            raise PlanCompilationError(
                f"task {task_key!r} has unsupported slots: {sorted(unknown)!r}"
            )

    def _resolve_slots(
        self,
        *,
        task_key: str,
        slots: tuple[UntrustedSlotCandidate, ...],
        actor: AccessContext,
    ) -> tuple[tuple[TypedSlot, ...], _ClarificationNeeded | None]:
        resolved: list[TypedSlot] = []
        for slot in slots:
            kind = _REFERENCE_KINDS.get(slot.name)
            list_kind = _LIST_REFERENCE_KINDS.get(slot.name)
            if kind is None and list_kind is None:
                resolved.append(self._user_slot(slot))
                continue

            try:
                if kind is not None:
                    reference = self._resolve_one(kind=kind, claimed=slot.value, actor=actor)
                    if reference is None:
                        return (), _ClarificationNeeded(
                            task_key=task_key,
                            slot_names=(slot.name,),
                            reason=ClarificationReason.UNTRUSTED_IDENTIFIER,
                        )
                    value: JsonValue = reference.canonical_value
                    source_reference = reference.source_reference
                else:
                    references = self._resolve_many(
                        kind=cast(str, list_kind),
                        claimed=slot.value,
                        actor=actor,
                    )
                    if references is None:
                        return (), _ClarificationNeeded(
                            task_key=task_key,
                            slot_names=(slot.name,),
                            reason=ClarificationReason.UNTRUSTED_IDENTIFIER,
                        )
                    value = [item.canonical_value for item in references]
                    source_reference = ",".join(item.source_reference for item in references)
            except PermissionError:
                return (), _ClarificationNeeded(
                    task_key=task_key,
                    slot_names=(slot.name,),
                    reason=ClarificationReason.UNAUTHORIZED_RESOURCE,
                )

            resolved.append(
                TypedSlot(
                    name=slot.name,
                    value_type=slot.value_type,
                    value=value,
                    provenance=SlotProvenance.TRUSTED_CONTEXT,
                    trust_level=SlotTrustLevel.TRUSTED_CONTEXT,
                    source_reference=source_reference,
                )
            )
        return tuple(resolved), None

    def _resolve_one(
        self,
        *,
        kind: str,
        claimed: JsonValue,
        actor: AccessContext,
    ) -> ResolvedReference | None:
        if not isinstance(claimed, str) or not claimed.strip():
            return None
        raw = self._reference_resolver.resolve(kind=kind, claimed_value=claimed, actor=actor)
        if raw is None:
            return None
        if not isinstance(raw, ResolvedReference):
            raise PlanCompilationError("reference resolver returned an invalid server object")
        return raw

    def _resolve_many(
        self,
        *,
        kind: str,
        claimed: JsonValue,
        actor: AccessContext,
    ) -> tuple[ResolvedReference, ...] | None:
        if not isinstance(claimed, list) or not claimed:
            return None
        references: list[ResolvedReference] = []
        for item in claimed:
            reference = self._resolve_one(kind=kind, claimed=item, actor=actor)
            if reference is None:
                return None
            references.append(reference)
        return tuple(references)

    @staticmethod
    def _user_slot(slot: UntrustedSlotCandidate) -> TypedSlot:
        return TypedSlot(
            name=slot.name,
            value_type=slot.value_type,
            value=slot.value,
            provenance=SlotProvenance.USER_REQUEST,
            trust_level=SlotTrustLevel.USER_ASSERTED,
        )

    @staticmethod
    def _validate_graph(candidate: ModelPlanCandidate) -> None:
        tasks = {task.task_key: task for task in candidate.tasks}
        adjacency: dict[str, list[str]] = {key: [] for key in tasks}
        for dependency in candidate.dependencies:
            source = tasks.get(dependency.from_task_key)
            destination = tasks.get(dependency.to_task_key)
            if source is None or destination is None:
                raise PlanCompilationError("dependency refers to an unknown task")
            if dependency.from_task_key == dependency.to_task_key:
                raise PlanCompilationError("dependency cannot be a self-edge")
            PlanCompiler._validate_output_binding(dependency, source.intent, destination.intent)
            adjacency[dependency.from_task_key].append(dependency.to_task_key)

        state = {key: 0 for key in tasks}

        def visit(key: str) -> None:
            if state[key] == 1:
                raise PlanCompilationError("workflow dependency graph contains a cycle")
            if state[key] == 2:
                return
            state[key] = 1
            for destination in adjacency[key]:
                visit(destination)
            state[key] = 2

        for task_key in tasks:
            visit(task_key)

    @staticmethod
    def _validate_output_binding(
        dependency: CandidateDependency,
        source_intent: IntentType,
        destination_intent: IntentType,
    ) -> None:
        binding = dependency.output_binding
        if dependency.kind is DependencyKind.DATA:
            if not binding:
                raise PlanCompilationError("data dependency requires an output binding")
        elif binding is not None:
            raise PlanCompilationError("only data dependencies may define an output binding")
        if binding is None:
            return

        source = resolve_capability_for_intent(source_intent)
        destination = get_intent_definition(destination_intent)
        if not set(binding).issubset(source.output_contract.required_fields) or not set(
            binding.values()
        ).issubset(destination.allowed_slots):
            raise PlanCompilationError("dependency output binding violates task contracts")

    @staticmethod
    def _clarification_request(
        *,
        workflow_id: UUID,
        plan_version: int,
        normalized_request_hash: str,
        need: _ClarificationNeeded,
    ) -> ClarificationRequest:
        request_id = uuid5(
            workflow_id,
            "clarification:"
            f"{plan_version}:{normalized_request_hash}:{need.task_key}:"
            f"{need.reason.value}:{','.join(need.slot_names)}",
        )
        task_id = uuid5(
            workflow_id,
            f"task:{plan_version}:{normalized_request_hash}:{need.task_key}",
        )
        return ClarificationRequest(
            id=request_id,
            workflow_id=workflow_id,
            task_id=task_id,
            expected_plan_version=plan_version,
            reason=need.reason,
            question="Provide an authorized, unambiguous value for the requested field(s).",
            slot_names=need.slot_names,
        )

    @staticmethod
    def _build_plan(
        *,
        workflow_id: UUID,
        plan_version: int,
        normalized_request_hash: str,
        planner_result: PlannerResult,
        tasks: tuple[_ValidatedTask, ...],
    ) -> CompilationOutcome:
        candidate_to_id = {
            task.candidate.task_key: uuid5(
                workflow_id,
                f"task:{plan_version}:{normalized_request_hash}:{task.candidate.task_key}",
            )
            for task in tasks
        }
        compiled: list[AtomicTask] = []
        operation_ids: list[UUID] = []
        approval_policies: dict[str, JsonValue] = {}
        payload_hashes: dict[str, JsonValue] = {}
        tool_keys: dict[str, JsonValue] = {}

        for position, validated in enumerate(tasks):
            candidate = validated.candidate
            task_key = candidate.task_key
            task_id = candidate_to_id[task_key]
            canonical_payload = {slot.name: slot.value for slot in validated.slots}
            side_effecting = validated.capability.side_effect.is_write
            task_operation_id = (
                operation_id(workflow_id, task_key, canonical_payload) if side_effecting else None
            )
            if task_operation_id is not None:
                operation_ids.append(task_operation_id)
            approval_policies[str(task_id)] = validated.capability.approval_policy.value
            payload_hashes[str(task_id)] = payload_hash(canonical_payload)
            tool_keys[str(task_id)] = validated.capability.tool_key
            compiled.append(
                AtomicTask(
                    id=task_id,
                    intent=candidate.intent,
                    source_span=candidate.source_span,
                    slots=validated.slots,
                    risk_level=validated.capability.default_risk,
                    side_effecting=side_effecting,
                    operation_id=task_operation_id,
                    position=position,
                )
            )

        dependencies = tuple(
            PlanDependency(
                from_task_id=candidate_to_id[dependency.from_task_key],
                to_task_id=candidate_to_id[dependency.to_task_key],
                kind=dependency.kind,
                output_binding=dependency.output_binding,
            )
            for dependency in planner_result.candidate.dependencies
        )
        plan_risk = max(
            (task.risk_level for task in compiled),
            key=_RISK_ORDER.__getitem__,
        )
        plan_id = uuid5(
            workflow_id,
            f"plan:{plan_version}:{normalized_request_hash}:{planner_result.catalog_version}",
        )
        plan = StructuredPlan(
            id=plan_id,
            workflow_id=workflow_id,
            version=plan_version,
            schema_version=planner_result.candidate.schema_version,
            source_model=planner_result.source_model,
            prompt_version=planner_result.prompt_version,
            normalized_request_hash=normalized_request_hash,
            status=PlanStatus.VALIDATED,
            risk_level=plan_risk,
            tasks=tuple(compiled),
            dependencies=dependencies,
            validation_summary={
                "catalog_version": planner_result.catalog_version,
                "approval_policies": approval_policies,
                "payload_hashes": payload_hashes,
                "tool_keys": tool_keys,
            },
        )
        return CompilationOutcome(
            plan=plan,
            clarification=None,
            operation_ids=tuple(operation_ids),
        )


__all__ = [
    "CompilationOutcome",
    "PlanCompilationError",
    "PlanCompiler",
    "ReferenceResolver",
    "ResolvedReference",
]
