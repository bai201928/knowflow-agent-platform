"""Versioned domain-event contracts and transport-independent messaging ports.

The broker only transports hints and minimal facts.  These models deliberately
reject credentials and large payloads before an event can reach an Outbox.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

MAX_EVENT_BYTES = 64 * 1024

WORKFLOW_TOPIC = "knowflow.workflow.v1"
DOCUMENT_TOPIC = "knowflow.documents.v1"
NOTIFICATION_TOPIC = "knowflow.notifications.v1"
SLA_TOPIC = "knowflow.sla.v1"
AUDIT_TOPIC = "knowflow.audit.v1"

WORKFLOW_CONSUMER_GROUP = "workflow-executors-v1"
DOCUMENT_CONSUMER_GROUP = "document-indexers-v1"
NOTIFICATION_CONSUMER_GROUP = "notification-senders-v1"
SLA_CONSUMER_GROUP = "sla-checkers-v1"
AUDIT_CONSUMER_GROUP = "audit-exporters-v1"

ALLOWED_PRODUCERS = frozenset(
    {
        "knowflow-api",
        "knowflow-cli",
        "knowflow-outbox",
        "knowflow-outbox-worker",
        "knowflow-workflow-worker",
        "knowflow-document-worker",
        "knowflow-notification-worker",
        "knowflow-sla-worker",
        "knowflow-audit-worker",
        "workflow-worker",
        "document-worker",
        "notification-worker",
        "sla-worker",
        "outbox-worker",
    }
)

_EVENT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9-]*)+$")
_UTC_RFC3339_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_TRACEPARENT_PATTERN = re.compile(
    r"^(?!ff)(?P<version>[0-9a-f]{2})-"
    r"(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<parent_id>[0-9a-f]{16})-"
    r"(?P<flags>[0-9a-f]{2})$"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "authorization",
        "bearer",
        "client_secret",
        "credential",
        "credentials",
        "document_body",
        "password",
        "private_key",
        "prompt",
        "secret",
        "token",
        "x_api_key",
    }
)


class EventContractError(Exception):
    """Base error for event contract handling."""


class TemporaryDependencyError(EventContractError):
    """A retryable database, broker, or network dependency failure."""


class PermanentEventError(EventContractError):
    """A permanently invalid event that must not enter a retry loop."""


class PayloadHashMismatchError(PermanentEventError):
    """The supplied hash does not identify the received canonical payload."""


class UnknownEventTypeError(PermanentEventError):
    """The event type is not in the local catalog."""


class UnsupportedSchemaVersionError(PermanentEventError):
    """The consumer cannot safely decode the event schema major version."""


class ObsoleteEventError(EventContractError):
    """The aggregate has advanced beyond an otherwise valid event."""


class ConsumerDisposition(StrEnum):
    """Transport action selected after local consumer processing."""

    ACK = "ACK"
    RETRY = "RETRY"
    QUARANTINE = "QUARANTINE"
    DEAD = "DEAD"


def _ensure_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be a timezone-aware UTC timestamp")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field_name} must use UTC")
    return value.astimezone(UTC)


def _normalized_key(value: object) -> str:
    return str(value).strip().lower().replace("-", "_")


def _reject_sensitive_keys(value: object, *, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = _normalized_key(key)
            if normalized in _SENSITIVE_KEYS or normalized.endswith("_password"):
                raise ValueError(f"sensitive field {path}.{key} is forbidden")
            if normalized.endswith("_token") or normalized.endswith("_secret"):
                raise ValueError(f"sensitive field {path}.{key} is forbidden")
            _reject_sensitive_keys(nested, path=f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, nested in enumerate(value):
            _reject_sensitive_keys(nested, path=f"{path}[{index}]")


def _json_compatible(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_none=False)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        normalized = _ensure_utc(value, field_name="hashed timestamp")
        return normalized.isoformat().replace("+00:00", "Z")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(nested) for key, nested in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_compatible(nested) for nested in value]
    return value


def _canonical_json(value: object) -> bytes:
    try:
        encoded = json.dumps(
            _json_compatible(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("event payload must be JSON serializable") from error
    return encoded


def canonical_payload_hash(payload: object) -> str:
    """Return the lowercase SHA-256 hash of canonical UTF-8 JSON."""

    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def validate_payload_hash(payload: object, expected_hash: str) -> None:
    """Verify a canonical payload hash without timing-sensitive comparison."""

    if not _SHA256_PATTERN.fullmatch(expected_hash):
        raise PayloadHashMismatchError("payload hash must be 64 lowercase hex characters")
    actual_hash = canonical_payload_hash(payload)
    if not hmac.compare_digest(actual_hash, expected_hash):
        raise PayloadHashMismatchError("payload hash mismatch")


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)


class _PayloadModel(_ContractModel):
    @model_validator(mode="before")
    @classmethod
    def reject_sensitive_payload_keys(cls, value: object) -> object:
        _reject_sensitive_keys(value)
        return value

    @field_validator("*", mode="before")
    @classmethod
    def reject_sensitive_nested_values(cls, value: object) -> object:
        _reject_sensitive_keys(value)
        return value


class AggregateRef(_ContractModel):
    type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    id: uuid.UUID
    version: int = Field(ge=1)


class TraceContext(_ContractModel):
    traceparent: str = Field(min_length=55, max_length=55)
    tracestate: str | None = Field(default=None, max_length=512)

    @field_validator("traceparent")
    @classmethod
    def validate_traceparent(cls, value: str) -> str:
        matched = _TRACEPARENT_PATTERN.fullmatch(value)
        if matched is None:
            raise ValueError("traceparent must follow the W3C lowercase hexadecimal format")
        if int(matched.group("trace_id"), 16) == 0 or int(matched.group("parent_id"), 16) == 0:
            raise ValueError("traceparent trace and parent identifiers must be non-zero")
        return value


class EventEnvelope(_ContractModel):
    message_id: uuid.UUID
    event_type: str = Field(min_length=3, max_length=128)
    schema_version: int = Field(ge=1)
    occurred_at: datetime
    producer: str = Field(min_length=1, max_length=128)
    aggregate: AggregateRef
    operation_id: uuid.UUID | None = None
    workflow_id: uuid.UUID | None = None
    trace: TraceContext
    payload: dict[str, Any]

    @field_validator("event_type")
    @classmethod
    def validate_event_type_syntax(cls, value: str) -> str:
        if _EVENT_TYPE_PATTERN.fullmatch(value) is None:
            raise ValueError("event_type must use lowercase dot notation")
        return value

    @field_validator("occurred_at", mode="before")
    @classmethod
    def validate_occurred_at_wire_format(cls, value: object) -> object:
        if isinstance(value, str) and _UTC_RFC3339_PATTERN.fullmatch(value) is None:
            raise ValueError("occurred_at must use RFC 3339 UTC Z format")
        return value

    @field_validator("occurred_at")
    @classmethod
    def validate_occurred_at(cls, value: datetime) -> datetime:
        return _ensure_utc(value, field_name="occurred_at")

    @field_validator("producer")
    @classmethod
    def validate_producer(cls, value: str) -> str:
        if value not in ALLOWED_PRODUCERS:
            raise ValueError("producer is not allowlisted")
        return value

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_sensitive_keys(value)
        if len(_canonical_json(value)) > MAX_EVENT_BYTES:
            raise ValueError(f"payload size exceeds {MAX_EVENT_BYTES} bytes")
        return value


class WorkflowCommandAcceptedPayload(_PayloadModel):
    command_id: str = Field(min_length=1, max_length=255)
    command_kind: str = Field(min_length=1, max_length=64)
    expected_workflow_version: int = Field(ge=1)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkflowReconciliationRequestedPayload(_PayloadModel):
    reason: str = Field(min_length=1, max_length=128)
    expected_workflow_version: int = Field(ge=1)
    observed_checkpoint_id: uuid.UUID | None = None


class DocumentIngestionRequestedPayload(_PayloadModel):
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    version: int = Field(ge=1)
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_location: str = Field(min_length=1, max_length=1024)
    media_type: str = Field(min_length=1, max_length=128)
    parser_version: str = Field(min_length=1, max_length=128)
    chunker_version: str = Field(min_length=1, max_length=128)
    embedding_version: str = Field(min_length=1, max_length=255)


class DocumentIngestionRetryRequestedPayload(_PayloadModel):
    document_version_id: uuid.UUID
    expected_status: str = Field(pattern=r"^FAILED$")
    failed_stage: str = Field(min_length=1, max_length=64)
    parser_version: str = Field(min_length=1, max_length=128)
    chunker_version: str = Field(min_length=1, max_length=128)
    embedding_version: str = Field(min_length=1, max_length=255)


class TicketCreatedPayload(_PayloadModel):
    ticket_id: uuid.UUID
    ticket_key: str = Field(min_length=1, max_length=64)
    ticket_version: int = Field(ge=1)
    severity: str = Field(pattern=r"^P[1-4]$")
    assigned_team_id: uuid.UUID
    notification_policy: str = Field(min_length=1, max_length=128)


class TicketUpdatedPayload(_PayloadModel):
    ticket_id: uuid.UUID
    ticket_key: str = Field(min_length=1, max_length=64)
    ticket_version: int = Field(ge=1)
    changed_fields: list[str] = Field(min_length=1)
    old_status: str | None = Field(default=None, max_length=64)
    new_status: str | None = Field(default=None, max_length=64)
    policy_version: int = Field(ge=1)


class ApprovalRequestedPayload(_PayloadModel):
    approval_id: uuid.UUID
    workflow_id: uuid.UUID
    action_type: str = Field(min_length=1, max_length=128)
    resource_type: str = Field(min_length=1, max_length=128)
    resource_id: str = Field(min_length=1, max_length=255)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    requester_user_id: uuid.UUID
    expires_at: datetime
    recipient_role: str = Field(min_length=1, max_length=64)

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, value: datetime) -> datetime:
        return _ensure_utc(value, field_name="expires_at")


class ApprovalDecidedPayload(_PayloadModel):
    approval_id: uuid.UUID
    decision_version: int = Field(ge=1)
    workflow_id: uuid.UUID
    command_id: str = Field(min_length=1, max_length=255)


class NotificationDeliveryRequestedPayload(_PayloadModel):
    delivery_id: uuid.UUID
    channel: str = Field(min_length=1, max_length=64)
    recipient_scope: str = Field(min_length=1, max_length=255)
    template: str = Field(min_length=1, max_length=128)
    template_version: int = Field(ge=1)
    data: dict[str, Any]

    @field_validator("data")
    @classmethod
    def validate_data_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_sensitive_keys(value, path="data")
        if len(_canonical_json(value)) > MAX_EVENT_BYTES:
            raise ValueError(f"notification data size exceeds {MAX_EVENT_BYTES} bytes")
        return value


class TicketSlaCheckRequestedPayload(_PayloadModel):
    ticket_id: uuid.UUID
    expected_sla_version: int = Field(ge=1)
    expected_deadline: datetime
    escalation_level: int = Field(ge=1)

    @field_validator("expected_deadline")
    @classmethod
    def validate_expected_deadline(cls, value: datetime) -> datetime:
        return _ensure_utc(value, field_name="expected_deadline")


@dataclass(frozen=True, slots=True)
class EventDefinition:
    topic: str
    consumer_group: str
    payload_model: type[BaseModel]
    supported_major: int = 1


EVENT_CATALOG: dict[str, EventDefinition] = {
    "workflow.command.accepted": EventDefinition(
        WORKFLOW_TOPIC, WORKFLOW_CONSUMER_GROUP, WorkflowCommandAcceptedPayload
    ),
    "workflow.reconciliation.requested": EventDefinition(
        WORKFLOW_TOPIC,
        WORKFLOW_CONSUMER_GROUP,
        WorkflowReconciliationRequestedPayload,
    ),
    "document.ingestion.requested": EventDefinition(
        DOCUMENT_TOPIC, DOCUMENT_CONSUMER_GROUP, DocumentIngestionRequestedPayload
    ),
    "document.ingestion.retry-requested": EventDefinition(
        DOCUMENT_TOPIC,
        DOCUMENT_CONSUMER_GROUP,
        DocumentIngestionRetryRequestedPayload,
    ),
    "ticket.created": EventDefinition(
        NOTIFICATION_TOPIC, NOTIFICATION_CONSUMER_GROUP, TicketCreatedPayload
    ),
    "ticket.updated": EventDefinition(
        NOTIFICATION_TOPIC, NOTIFICATION_CONSUMER_GROUP, TicketUpdatedPayload
    ),
    "approval.requested": EventDefinition(
        NOTIFICATION_TOPIC, NOTIFICATION_CONSUMER_GROUP, ApprovalRequestedPayload
    ),
    "approval.decided": EventDefinition(
        WORKFLOW_TOPIC, WORKFLOW_CONSUMER_GROUP, ApprovalDecidedPayload
    ),
    "notification.delivery.requested": EventDefinition(
        NOTIFICATION_TOPIC,
        NOTIFICATION_CONSUMER_GROUP,
        NotificationDeliveryRequestedPayload,
    ),
    "ticket.sla.check-requested": EventDefinition(
        SLA_TOPIC, SLA_CONSUMER_GROUP, TicketSlaCheckRequestedPayload
    ),
}


@dataclass(frozen=True, slots=True)
class ParsedEvent:
    envelope: EventEnvelope
    payload: BaseModel
    topic: str
    consumer_group: str


def is_schema_compatible(schema_version: int, supported_major: int) -> bool:
    """Return whether a consumer can read an event's integer major schema."""

    return schema_version > 0 and schema_version == supported_major


def parse_event(data: Mapping[str, Any] | EventEnvelope) -> ParsedEvent:
    """Validate an envelope, route it, and decode its versioned payload.

    Type and major-version checks intentionally happen before payload decoding so
    unknown future schemas cannot trigger business validation or side effects.
    """

    raw: Mapping[str, Any]
    if isinstance(data, EventEnvelope):
        raw = data.model_dump(mode="python")
    else:
        raw = data

    event_type = raw.get("event_type")
    if not isinstance(event_type, str) or event_type not in EVENT_CATALOG:
        raise UnknownEventTypeError(f"unknown event type: {event_type!r}")

    definition = EVENT_CATALOG[event_type]
    schema_version = raw.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise PermanentEventError("schema_version must be a positive integer")
    if not is_schema_compatible(schema_version, definition.supported_major):
        raise UnsupportedSchemaVersionError(
            f"unsupported schema major {schema_version} for {event_type}; "
            f"supported major is {definition.supported_major}"
        )

    try:
        envelope = data if isinstance(data, EventEnvelope) else EventEnvelope.model_validate(raw)
        payload = definition.payload_model.model_validate(envelope.payload)
    except ValidationError as error:
        raise PermanentEventError(f"malformed {event_type} event") from error

    return ParsedEvent(
        envelope=envelope,
        payload=payload,
        topic=definition.topic,
        consumer_group=definition.consumer_group,
    )


def classify_consumer_failure(error: BaseException) -> ConsumerDisposition:
    """Map failures to bounded broker behavior, preferring no poison loops."""

    if isinstance(error, (UnknownEventTypeError, UnsupportedSchemaVersionError)):
        return ConsumerDisposition.QUARANTINE
    if isinstance(error, ObsoleteEventError):
        return ConsumerDisposition.ACK
    if isinstance(error, (TemporaryDependencyError, TimeoutError, ConnectionError)):
        return ConsumerDisposition.RETRY
    return ConsumerDisposition.DEAD


@runtime_checkable
class EventPublisher(Protocol):
    async def publish(self, event: EventEnvelope) -> None:
        """Publish an envelope while preserving its logical message ID."""


@runtime_checkable
class EventConsumer(Protocol):
    async def consume(self, event: EventEnvelope) -> ConsumerDisposition:
        """Apply or classify a validated event without duplicating local effects."""
