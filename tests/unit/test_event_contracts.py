from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from knowflow.domain.messaging.events import (
    EVENT_CATALOG,
    MAX_EVENT_BYTES,
    AggregateRef,
    ConsumerDisposition,
    EventConsumer,
    EventEnvelope,
    EventPublisher,
    PayloadHashMismatchError,
    PermanentEventError,
    TemporaryDependencyError,
    TraceContext,
    UnknownEventTypeError,
    UnsupportedSchemaVersionError,
    canonical_payload_hash,
    classify_consumer_failure,
    parse_event,
    validate_payload_hash,
)

MESSAGE_ID = uuid.UUID("4a913e67-47bd-4d60-a8d6-9cd9ba80a900")
AGGREGATE_ID = uuid.UUID("f571f013-00df-49e6-a713-d69fdc838687")
OPERATION_ID = uuid.UUID("2d8e3d42-e56a-5ab2-b55a-d974575b7bbf")
WORKFLOW_ID = uuid.UUID("f787084b-1710-4295-a5e7-479808ff9f43")

EXPECTED_ROUTES = {
    "workflow.command.accepted": ("knowflow.workflow.v1", "workflow-executors-v1"),
    "workflow.reconciliation.requested": (
        "knowflow.workflow.v1",
        "workflow-executors-v1",
    ),
    "document.ingestion.requested": ("knowflow.documents.v1", "document-indexers-v1"),
    "document.ingestion.retry-requested": (
        "knowflow.documents.v1",
        "document-indexers-v1",
    ),
    "ticket.created": ("knowflow.notifications.v1", "notification-senders-v1"),
    "ticket.updated": ("knowflow.notifications.v1", "notification-senders-v1"),
    "approval.requested": ("knowflow.notifications.v1", "notification-senders-v1"),
    "approval.decided": ("knowflow.workflow.v1", "workflow-executors-v1"),
    "notification.delivery.requested": (
        "knowflow.notifications.v1",
        "notification-senders-v1",
    ),
    "ticket.sla.check-requested": ("knowflow.sla.v1", "sla-checkers-v1"),
}

PAYLOAD_EXAMPLES: dict[str, dict[str, Any]] = {
    "workflow.command.accepted": {
        "command_id": "approval-id:decision-version",
        "command_kind": "APPROVAL_RESUME",
        "expected_workflow_version": 8,
        "payload_hash": "a" * 64,
    },
    "workflow.reconciliation.requested": {
        "reason": "CHECKPOINT_MISSING",
        "expected_workflow_version": 12,
        "observed_checkpoint_id": None,
    },
    "document.ingestion.requested": {
        "document_id": str(AGGREGATE_ID),
        "document_version_id": str(uuid.uuid4()),
        "version": 1,
        "checksum_sha256": "b" * 64,
        "source_location": "data/seed/rocketmq/manual.md",
        "media_type": "text/markdown",
        "parser_version": "parser-v1",
        "chunker_version": "chunker-v1",
        "embedding_version": "stub:hash:384",
    },
    "document.ingestion.retry-requested": {
        "document_version_id": str(uuid.uuid4()),
        "expected_status": "FAILED",
        "failed_stage": "INDEX",
        "parser_version": "parser-v1",
        "chunker_version": "chunker-v1",
        "embedding_version": "stub:hash:384",
    },
    "ticket.created": {
        "ticket_id": str(AGGREGATE_ID),
        "ticket_key": "INC-000001",
        "ticket_version": 1,
        "severity": "P1",
        "assigned_team_id": str(uuid.uuid4()),
        "notification_policy": "ticket-created-v1",
    },
    "ticket.updated": {
        "ticket_id": str(AGGREGATE_ID),
        "ticket_key": "INC-000001",
        "ticket_version": 2,
        "changed_fields": ["status"],
        "old_status": "OPEN",
        "new_status": "ACKNOWLEDGED",
        "policy_version": 1,
    },
    "approval.requested": {
        "approval_id": str(uuid.uuid4()),
        "workflow_id": str(WORKFLOW_ID),
        "action_type": "consumer.restart.sandbox",
        "resource_type": "sandbox_consumer",
        "resource_id": "demo-consumer-a",
        "payload_hash": "c" * 64,
        "requester_user_id": str(uuid.uuid4()),
        "expires_at": "2026-08-03T01:00:00Z",
        "recipient_role": "APPROVER",
    },
    "approval.decided": {
        "approval_id": str(uuid.uuid4()),
        "decision_version": 1,
        "workflow_id": str(WORKFLOW_ID),
        "command_id": "approval-id:decision-version",
    },
    "notification.delivery.requested": {
        "delivery_id": str(uuid.uuid4()),
        "channel": "MAIL_CAPTURE",
        "recipient_scope": "team:noc",
        "template": "p1-ticket-created",
        "template_version": 1,
        "data": {"ticket_key": "INC-000001"},
    },
    "ticket.sla.check-requested": {
        "ticket_id": str(AGGREGATE_ID),
        "expected_sla_version": 2,
        "expected_deadline": "2026-08-03T02:00:00Z",
        "escalation_level": 1,
    },
}


def envelope_data(
    *,
    event_type: str = "ticket.created",
    schema_version: int = 1,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "message_id": str(MESSAGE_ID),
        "event_type": event_type,
        "schema_version": schema_version,
        "occurred_at": "2026-08-03T00:00:00Z",
        "producer": "knowflow-api",
        "aggregate": {"type": "ticket", "id": str(AGGREGATE_ID), "version": 1},
        "operation_id": str(OPERATION_ID),
        "workflow_id": str(WORKFLOW_ID),
        "trace": {
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            "tracestate": None,
        },
        "payload": payload if payload is not None else PAYLOAD_EXAMPLES[event_type],
    }


def test_envelope_models_are_versioned_pydantic_contracts() -> None:
    envelope = EventEnvelope.model_validate(envelope_data())

    assert issubclass(EventEnvelope, BaseModel)
    assert issubclass(AggregateRef, BaseModel)
    assert issubclass(TraceContext, BaseModel)
    assert envelope.message_id == MESSAGE_ID
    assert envelope.schema_version == 1
    assert envelope.aggregate == AggregateRef(type="ticket", id=AGGREGATE_ID, version=1)
    assert envelope.occurred_at == datetime(2026, 8, 3, tzinfo=UTC)
    assert envelope.trace.traceparent.startswith("00-")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("message_id", "not-a-uuid"),
        ("schema_version", 0),
        ("occurred_at", "2026-08-03T00:00:00"),
        ("occurred_at", "2026-08-03 00:00:00Z"),
        ("occurred_at", "2026-08-03T00:00:00+00:00"),
        ("occurred_at", "2026-08-03T08:00:00+08:00"),
        ("producer", "untrusted-process"),
    ],
)
def test_envelope_rejects_invalid_identity_version_time_and_producer(
    field: str, value: object
) -> None:
    data = envelope_data()
    data[field] = value

    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(data)


@pytest.mark.parametrize(
    "occurred_at",
    ["2026-08-03T00:00:00Z", "2026-08-03T00:00:00.123456Z"],
)
def test_envelope_accepts_strict_rfc3339_utc_z_timestamps(occurred_at: str) -> None:
    data = envelope_data()
    data["occurred_at"] = occurred_at

    envelope = EventEnvelope.model_validate(data)

    assert envelope.occurred_at.tzinfo is UTC


def test_event_catalog_is_complete_and_routes_every_payload_schema() -> None:
    assert set(EVENT_CATALOG) == set(EXPECTED_ROUTES)

    for event_type, (topic, consumer_group) in EXPECTED_ROUTES.items():
        definition = EVENT_CATALOG[event_type]
        assert definition.topic == topic
        assert definition.consumer_group == consumer_group
        assert definition.supported_major == 1
        payload = definition.payload_model.model_validate(PAYLOAD_EXAMPLES[event_type])
        assert isinstance(payload, BaseModel)


def test_same_major_allows_additive_payload_fields() -> None:
    data = envelope_data()
    data["payload"] = {**PAYLOAD_EXAMPLES["ticket.created"], "new_optional_fact": "ok"}

    parsed = parse_event(data)

    assert parsed.envelope.schema_version == 1
    assert parsed.payload.ticket_key == "INC-000001"


@pytest.mark.parametrize(
    "event_type",
    ["Ticket.Created", "ticket_created", "ticket..created", "not.in.catalog"],
)
def test_unknown_or_noncanonical_event_type_is_quarantined(event_type: str) -> None:
    with pytest.raises(UnknownEventTypeError) as captured:
        parse_event(envelope_data(event_type=event_type, payload={}))

    assert classify_consumer_failure(captured.value) is ConsumerDisposition.QUARANTINE


def test_unsupported_major_schema_is_quarantined_without_payload_processing() -> None:
    with pytest.raises(UnsupportedSchemaVersionError) as captured:
        parse_event(envelope_data(schema_version=2))

    assert classify_consumer_failure(captured.value) is ConsumerDisposition.QUARANTINE


@pytest.mark.parametrize(
    "secret_key",
    [
        "api_key",
        "x-api-key",
        "x_api_key",
        "password",
        "private_key",
        "bearer",
        "authorization",
        "access_token",
        "client_secret",
        "token",
    ],
)
def test_payload_rejects_sensitive_keys_at_any_depth(secret_key: str) -> None:
    data = envelope_data(payload={"safe": [{"nested": [{secret_key: "must-not-publish"}]}]})

    with pytest.raises(ValidationError, match="sensitive"):
        EventEnvelope.model_validate(data)


def test_payload_allows_an_ordinary_business_key_field() -> None:
    data = envelope_data(payload={"routing": [{"key": "incident-category"}]})

    envelope = EventEnvelope.model_validate(data)

    assert envelope.payload["routing"][0]["key"] == "incident-category"


def test_payload_size_is_bounded_before_publication() -> None:
    data = envelope_data(payload={"value": "x" * (MAX_EVENT_BYTES + 1)})

    with pytest.raises(ValidationError, match="size"):
        EventEnvelope.model_validate(data)


def test_canonical_payload_hash_is_order_independent_and_validated() -> None:
    left = {"ticket": {"severity": "P1", "version": 1}, "recipients": ["ops"]}
    same = {"recipients": ["ops"], "ticket": {"version": 1, "severity": "P1"}}
    changed = {"recipients": ["ops"], "ticket": {"version": 2, "severity": "P1"}}

    expected = canonical_payload_hash(left)
    assert len(expected) == 64
    assert expected == canonical_payload_hash(same)
    assert expected != canonical_payload_hash(changed)
    validate_payload_hash(same, expected)

    with pytest.raises(PayloadHashMismatchError):
        validate_payload_hash(changed, expected)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TemporaryDependencyError("database timeout"), ConsumerDisposition.RETRY),
        (PermanentEventError("malformed payload"), ConsumerDisposition.DEAD),
        (PayloadHashMismatchError("payload hash mismatch"), ConsumerDisposition.DEAD),
    ],
)
def test_consumer_failure_classification_prevents_poison_loops(
    error: Exception, expected: ConsumerDisposition
) -> None:
    assert classify_consumer_failure(error) is expected


class PublisherStub:
    async def publish(self, event: EventEnvelope) -> None:
        del event


class ConsumerStub:
    async def consume(self, event: EventEnvelope) -> ConsumerDisposition:
        del event
        return ConsumerDisposition.ACK


def test_async_publisher_and_consumer_ports_are_runtime_checkable() -> None:
    publisher = PublisherStub()
    consumer = ConsumerStub()

    assert isinstance(publisher, EventPublisher)
    assert isinstance(consumer, EventConsumer)
    assert inspect.iscoroutinefunction(publisher.publish)
    assert inspect.iscoroutinefunction(consumer.consume)
