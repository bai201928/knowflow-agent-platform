"""Immutable intent and capability catalog for deterministic plan compilation.

The model may select only an intent.  Tool selection, authorization, deadlines, and
approval boundaries are owned by this server-side catalog.  In particular, no entry
accepts a URL, SQL, shell command, Python source, or dynamically named tool.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from knowflow.domain.workflows.schemas import IntentType, RiskLevel
from knowflow.infrastructure.db.models.identity import RoleCode

INTENT_CATALOG_VERSION: Final = "1.0.0"
_TOOL_KEY_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_FORBIDDEN_TOOL_TERMS: Final = frozenset(
    {"command", "code", "exec", "http", "python", "script", "shell", "sql", "url"}
)
_FORBIDDEN_DYNAMIC_SLOTS: Final = frozenset(
    {"command", "code", "python", "script", "shell", "sql", "statement", "url"}
)


class Capability(StrEnum):
    """Stable business capabilities understood by the deterministic compiler."""

    KNOWLEDGE_SEARCH = "knowledge.search"
    TICKET_CREATE = "ticket.create"
    TICKET_READ = "ticket.read"
    TICKET_UPDATE = "ticket.update"
    NOTIFICATION_REGISTER = "notification.register"
    CONSUMER_RESTART = "operations.consumer_restart"


class SideEffect(StrEnum):
    """Observable effect class used to select reliability controls."""

    NONE = "NONE"
    DURABLE_WRITE = "DURABLE_WRITE"
    OUTBOX_REGISTRATION = "OUTBOX_REGISTRATION"
    SANDBOX_OPERATION = "SANDBOX_OPERATION"

    @property
    def is_write(self) -> bool:
        return self is not SideEffect.NONE


class ApprovalPolicy(StrEnum):
    """Server-owned approval boundary; model risk claims cannot weaken it."""

    NEVER = "NEVER"
    CONDITIONAL = "CONDITIONAL"
    ALWAYS = "ALWAYS"


@dataclass(frozen=True, slots=True)
class OutputContract:
    """Minimal named result boundary passed between compiled tasks."""

    schema_key: str
    schema_version: int
    required_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _TOOL_KEY_PATTERN.fullmatch(self.schema_key):
            raise ValueError("output schema_key must be a namespaced server key")
        if self.schema_version < 1:
            raise ValueError("output schema_version must be positive")
        if not self.required_fields or len(set(self.required_fields)) != len(self.required_fields):
            raise ValueError("output required_fields must be non-empty and unique")


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    """An allowlisted adapter boundary with fixed security and execution policy."""

    key: Capability
    tool_key: str
    permitted_roles: frozenset[RoleCode]
    side_effect: SideEffect
    default_deadline_seconds: int
    default_risk: RiskLevel
    approval_policy: ApprovalPolicy
    output_contract: OutputContract

    def __post_init__(self) -> None:
        if not _TOOL_KEY_PATTERN.fullmatch(self.tool_key):
            raise ValueError("tool_key must be a namespaced server allowlist key")
        terms = frozenset(self.tool_key.replace("_", ".").split("."))
        if terms.intersection(_FORBIDDEN_TOOL_TERMS):
            raise ValueError("tool_key may not expose arbitrary execution primitives")
        if not self.permitted_roles:
            raise ValueError("a capability must grant at least one server-derived role")
        if self.default_deadline_seconds < 1:
            raise ValueError("default_deadline_seconds must be positive")
        if not self.side_effect.is_write and self.approval_policy is not ApprovalPolicy.NEVER:
            raise ValueError("read-only capabilities cannot require write approval")
        if self.approval_policy is ApprovalPolicy.ALWAYS and not self.side_effect.is_write:
            raise ValueError("always-approved capabilities must have a side effect")


@dataclass(frozen=True, slots=True)
class IntentDefinition:
    """Versioned intent boundary exposed to structured model planning."""

    intent: IntentType
    catalog_version: str
    capability: Capability
    required_slots: frozenset[str]
    optional_slots: frozenset[str]

    def __post_init__(self) -> None:
        if self.catalog_version != INTENT_CATALOG_VERSION:
            raise ValueError("intent definition uses an unsupported catalog version")
        if self.required_slots.intersection(self.optional_slots):
            raise ValueError("required and optional slots must be disjoint")
        slots = self.required_slots.union(self.optional_slots)
        if any(not slot.isidentifier() for slot in slots):
            raise ValueError("slot names must be Python-style identifiers")
        if slots.intersection(_FORBIDDEN_DYNAMIC_SLOTS):
            raise ValueError("catalog entries cannot accept dynamic execution inputs")

    @property
    def allowed_slots(self) -> frozenset[str]:
        return self.required_slots.union(self.optional_slots)


class CatalogLookupError(ValueError):
    """Raised when a plan references an unsupported catalog value."""


class SlotValidationError(CatalogLookupError):
    """Raised when provided slots do not match the fixed intent boundary."""


_ALL_INTERACTIVE_ROLES: Final = frozenset(RoleCode)
_EMPLOYEE_WRITE_ROLES: Final = frozenset({RoleCode.EMPLOYEE, RoleCode.OPERATOR, RoleCode.ADMIN})
_OPERATION_ROLES: Final = frozenset({RoleCode.OPERATOR, RoleCode.ADMIN})

CAPABILITY_REGISTRY: Final[Mapping[Capability, CapabilityDefinition]] = MappingProxyType(
    {
        Capability.KNOWLEDGE_SEARCH: CapabilityDefinition(
            key=Capability.KNOWLEDGE_SEARCH,
            tool_key="knowledge.search_answer",
            permitted_roles=_ALL_INTERACTIVE_ROLES,
            side_effect=SideEffect.NONE,
            default_deadline_seconds=15,
            default_risk=RiskLevel.LOW,
            approval_policy=ApprovalPolicy.NEVER,
            output_contract=OutputContract(
                "knowledge.answer", 1, ("answer", "disposition", "citations")
            ),
        ),
        Capability.TICKET_CREATE: CapabilityDefinition(
            key=Capability.TICKET_CREATE,
            tool_key="tickets.create",
            permitted_roles=_EMPLOYEE_WRITE_ROLES,
            side_effect=SideEffect.DURABLE_WRITE,
            default_deadline_seconds=30,
            default_risk=RiskLevel.MEDIUM,
            approval_policy=ApprovalPolicy.NEVER,
            output_contract=OutputContract(
                "tickets.created", 1, ("ticket_id", "key", "version", "status")
            ),
        ),
        Capability.TICKET_READ: CapabilityDefinition(
            key=Capability.TICKET_READ,
            tool_key="tickets.read",
            permitted_roles=_ALL_INTERACTIVE_ROLES,
            side_effect=SideEffect.NONE,
            default_deadline_seconds=10,
            default_risk=RiskLevel.LOW,
            approval_policy=ApprovalPolicy.NEVER,
            output_contract=OutputContract("tickets.result", 1, ("items",)),
        ),
        Capability.TICKET_UPDATE: CapabilityDefinition(
            key=Capability.TICKET_UPDATE,
            tool_key="tickets.update",
            permitted_roles=_EMPLOYEE_WRITE_ROLES,
            side_effect=SideEffect.DURABLE_WRITE,
            default_deadline_seconds=30,
            default_risk=RiskLevel.MEDIUM,
            approval_policy=ApprovalPolicy.CONDITIONAL,
            output_contract=OutputContract(
                "tickets.updated", 1, ("ticket_id", "version", "status")
            ),
        ),
        Capability.NOTIFICATION_REGISTER: CapabilityDefinition(
            key=Capability.NOTIFICATION_REGISTER,
            tool_key="notifications.register",
            permitted_roles=_EMPLOYEE_WRITE_ROLES,
            side_effect=SideEffect.OUTBOX_REGISTRATION,
            default_deadline_seconds=20,
            default_risk=RiskLevel.MEDIUM,
            approval_policy=ApprovalPolicy.NEVER,
            output_contract=OutputContract(
                "notifications.registered", 1, ("delivery_id", "status")
            ),
        ),
        Capability.CONSUMER_RESTART: CapabilityDefinition(
            key=Capability.CONSUMER_RESTART,
            tool_key="operations.restart_consumer",
            permitted_roles=_OPERATION_ROLES,
            side_effect=SideEffect.SANDBOX_OPERATION,
            default_deadline_seconds=60,
            default_risk=RiskLevel.HIGH,
            approval_policy=ApprovalPolicy.ALWAYS,
            output_contract=OutputContract(
                "operations.consumer_restart", 1, ("operation_id", "status", "target")
            ),
        ),
    }
)

INTENT_CATALOG: Final[Mapping[IntentType, IntentDefinition]] = MappingProxyType(
    {
        IntentType.KNOWLEDGE_QUERY: IntentDefinition(
            IntentType.KNOWLEDGE_QUERY,
            INTENT_CATALOG_VERSION,
            Capability.KNOWLEDGE_SEARCH,
            frozenset({"query"}),
            frozenset({"collection_ids", "max_citations"}),
        ),
        IntentType.TICKET_CREATE: IntentDefinition(
            IntentType.TICKET_CREATE,
            INTENT_CATALOG_VERSION,
            Capability.TICKET_CREATE,
            frozenset({"title", "description", "severity"}),
            frozenset({"assigned_team_id"}),
        ),
        IntentType.TICKET_QUERY: IntentDefinition(
            IntentType.TICKET_QUERY,
            INTENT_CATALOG_VERSION,
            Capability.TICKET_READ,
            frozenset(),
            frozenset({"ticket_id", "status", "severity", "assigned_team_id", "cursor"}),
        ),
        IntentType.TICKET_UPDATE: IntentDefinition(
            IntentType.TICKET_UPDATE,
            INTENT_CATALOG_VERSION,
            Capability.TICKET_UPDATE,
            frozenset({"ticket_id", "expected_version"}),
            frozenset(
                {
                    "status",
                    "severity",
                    "assignee_user_id",
                    "assigned_team_id",
                    "comment",
                }
            ),
        ),
        IntentType.NOTIFICATION_SEND: IntentDefinition(
            IntentType.NOTIFICATION_SEND,
            INTENT_CATALOG_VERSION,
            Capability.NOTIFICATION_REGISTER,
            frozenset({"recipient_scope", "template_key"}),
            frozenset({"channel", "template_data", "ticket_id", "workflow_id"}),
        ),
        IntentType.OPS_ACTION: IntentDefinition(
            IntentType.OPS_ACTION,
            INTENT_CATALOG_VERSION,
            Capability.CONSUMER_RESTART,
            frozenset({"consumer_group", "reason"}),
            frozenset({"expected_status", "ticket_id"}),
        ),
    }
)

_TOOL_REGISTRY: Final[Mapping[str, CapabilityDefinition]] = MappingProxyType(
    {definition.tool_key: definition for definition in CAPABILITY_REGISTRY.values()}
)


def get_intent_definition(
    intent: IntentType | str, *, catalog_version: str = INTENT_CATALOG_VERSION
) -> IntentDefinition:
    """Return an exact catalog entry, rejecting unknown intent or catalog versions."""

    if catalog_version != INTENT_CATALOG_VERSION:
        raise CatalogLookupError(f"unsupported intent catalog version: {catalog_version!r}")
    try:
        intent_type = intent if isinstance(intent, IntentType) else IntentType(intent)
        return INTENT_CATALOG[intent_type]
    except (KeyError, ValueError) as exc:
        raise CatalogLookupError(f"unsupported intent: {intent!r}") from exc


def get_capability_definition(
    capability: Capability | str,
) -> CapabilityDefinition:
    """Return an exact server-defined capability; fabricated names fail closed."""

    try:
        capability_key = (
            capability if isinstance(capability, Capability) else Capability(capability)
        )
        return CAPABILITY_REGISTRY[capability_key]
    except (KeyError, ValueError) as exc:
        raise CatalogLookupError(f"unsupported capability: {capability!r}") from exc


def get_tool_definition(tool_key: str) -> CapabilityDefinition:
    """Resolve only a fixed allowlisted tool key; never resolve imports, URLs, or code."""

    try:
        return _TOOL_REGISTRY[tool_key]
    except KeyError as exc:
        raise CatalogLookupError(f"tool is not allowlisted: {tool_key!r}") from exc


def resolve_capability_for_intent(intent: IntentType | str) -> CapabilityDefinition:
    """Resolve the sole server-selected capability for an intent."""

    return get_capability_definition(get_intent_definition(intent).capability)


def missing_required_slots(
    intent: IntentType | str, provided_slots: Mapping[str, object] | Iterable[str]
) -> frozenset[str]:
    """Return required keys absent from the bounded slot payload."""

    definition = get_intent_definition(intent)
    if isinstance(provided_slots, Mapping):
        present = {key for key, value in provided_slots.items() if value is not None}
    elif isinstance(provided_slots, str):
        raise SlotValidationError("provided_slots must not be a string")
    else:
        present = set(provided_slots)
    return definition.required_slots.difference(present)


def validate_slots(
    intent: IntentType | str, provided_slots: Mapping[str, object] | Iterable[str]
) -> None:
    """Reject missing or undeclared slots before any capability can run."""

    definition = get_intent_definition(intent)
    if isinstance(provided_slots, Mapping):
        provided = set(provided_slots)
    elif isinstance(provided_slots, str):
        raise SlotValidationError("provided_slots must not be a string")
    else:
        provided = set(provided_slots)
    unknown = provided.difference(definition.allowed_slots)
    if unknown:
        raise SlotValidationError(f"unsupported slots: {sorted(unknown)!r}")
    missing = missing_required_slots(intent, provided_slots)
    if missing:
        raise SlotValidationError(f"missing required slots: {sorted(missing)!r}")


def role_is_permitted(capability: Capability | str, role: RoleCode) -> bool:
    """Check one trusted server-derived role against the immutable registry."""

    return role in get_capability_definition(capability).permitted_roles


def require_permitted_role(capability: Capability | str, role: RoleCode) -> None:
    """Fail closed when the trusted role lacks the requested capability."""

    if not role_is_permitted(capability, role):
        raise PermissionError(f"role {role.value} cannot use capability {capability!s}")


def validate_catalog() -> None:
    """Assert the static catalog is complete, internally linked, and closed."""

    if set(INTENT_CATALOG) != set(IntentType):
        raise RuntimeError("intent catalog must define exactly the six supported intents")
    if set(CAPABILITY_REGISTRY) != set(Capability):
        raise RuntimeError("capability registry is incomplete")
    if len(_TOOL_REGISTRY) != len(CAPABILITY_REGISTRY):
        raise RuntimeError("every capability must have a unique allowlisted tool key")
    for intent_key, intent_definition in INTENT_CATALOG.items():
        if intent_definition.intent is not intent_key:
            raise RuntimeError("intent catalog key does not match its definition")
        if intent_definition.capability not in CAPABILITY_REGISTRY:
            raise RuntimeError("intent references an unknown capability")
    for capability_key, capability_definition in CAPABILITY_REGISTRY.items():
        if capability_definition.key is not capability_key:
            raise RuntimeError("capability registry key does not match its definition")


validate_catalog()
