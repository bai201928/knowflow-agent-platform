"""Foundation-level contract checks for KnowFlow's published interfaces.

These tests intentionally inspect the source contracts rather than a generated
client.  They catch broken local references and accidental removal or weakening
of cross-cutting API/event guarantees before application code is exercised.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_DIR = ROOT / "specs" / "001-knowflow-agent-platform" / "contracts"
OPENAPI_PATH = CONTRACT_DIR / "openapi.yaml"
EVENTS_PATH = CONTRACT_DIR / "events.md"
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    document = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "OpenAPI contract must be a YAML mapping"
    return document


def _walk(value: Any) -> Iterator[Any]:
    """Yield every nested mapping/list member, including *value* itself."""

    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _resolve_json_pointer(document: dict[str, Any], reference: str) -> Any:
    assert reference.startswith("#/"), f"only local OpenAPI refs are allowed: {reference}"
    current: Any = document
    for token in reference[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        assert isinstance(current, dict) and token in current, (
            f"unresolved OpenAPI ref: {reference}"
        )
        current = current[token]
    return current


def _operation(openapi: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    assert path in openapi["paths"], f"required endpoint is absent: {path}"
    path_item = openapi["paths"][path]
    assert method in path_item, f"required operation is absent: {method.upper()} {path}"
    return path_item[method]


def test_openapi_31_and_all_local_references_resolve(openapi: dict[str, Any]) -> None:
    assert str(openapi["openapi"]).startswith("3.1.")

    references = [
        node["$ref"] for node in _walk(openapi) if isinstance(node, dict) and "$ref" in node
    ]
    assert references, "contract should reuse named OpenAPI components"
    for reference in references:
        _resolve_json_pointer(openapi, reference)


def test_operation_ids_are_present_and_unique(openapi: dict[str, Any]) -> None:
    operations = [
        operation
        for path_item in openapi["paths"].values()
        for method, operation in path_item.items()
        if method in HTTP_METHODS
    ]
    operation_ids = [operation.get("operationId") for operation in operations]

    assert all(operation_ids), "every HTTP operation must define operationId"
    duplicates = sorted({item for item in operation_ids if operation_ids.count(item) > 1})
    assert not duplicates, f"operationId values must be unique: {duplicates}"


@pytest.mark.parametrize(
    ("path", "method", "operation_id"),
    [
        # 1A: immutable document version and explicit retry contract.
        ("/documents/{documentId}/versions", "post", "createDocumentVersion"),
        ("/documents/{documentId}/versions/{versionId}", "get", "getDocumentVersion"),
        ("/documents/{documentId}/versions/{versionId}/retry", "post", "retryDocumentVersion"),
        # 2A: resource-scoped audit timelines.
        ("/workflows/{workflowId}/audit-events", "get", "listWorkflowAuditEvents"),
        ("/tickets/{ticketId}/audit-events", "get", "listTicketAuditEvents"),
        # 3A: operator recovery review and decision.
        ("/workflows/{workflowId}/recovery-review", "get", "getWorkflowRecoveryReview"),
        ("/workflows/{workflowId}/recovery-decisions", "post", "decideWorkflowRecovery"),
        # 4C: dedicated notification delivery status queries.
        ("/notification-deliveries", "get", "listNotificationDeliveries"),
        ("/notification-deliveries/{deliveryId}", "get", "getNotificationDelivery"),
    ],
)
def test_high_priority_contract_operations_exist(
    openapi: dict[str, Any], path: str, method: str, operation_id: str
) -> None:
    operation = _operation(openapi, path, method)
    assert operation["operationId"] == operation_id


def test_recovery_decision_is_versioned_idempotent_and_auditable(openapi: dict[str, Any]) -> None:
    operation = _operation(openapi, "/workflows/{workflowId}/recovery-decisions", "post")
    parameter_refs = {item.get("$ref") for item in operation["parameters"]}
    assert "#/components/parameters/IdempotencyKey" in parameter_refs

    request_ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    request_schema = _resolve_json_pointer(openapi, request_ref)
    assert set(request_schema["required"]) == {"action", "expected_workflow_version", "reason"}
    assert set(openapi["components"]["schemas"]["RecoveryAction"]["enum"]) == {
        "RESUME_FROM_FACTS",
        "RETRY_SAFE_STEP",
        "MARK_FAILED",
        "REQUIRE_NEW_APPROVAL",
    }


def test_cross_cutting_status_enums_remain_explicit(openapi: dict[str, Any]) -> None:
    schemas = openapi["components"]["schemas"]
    assert "NEEDS_REVIEW" in schemas["WorkflowStatus"]["enum"]
    assert set(schemas["NotificationDeliveryStatus"]["enum"]) == {
        "PENDING",
        "SENDING",
        "DELIVERED",
        "RETRYING",
        "UNKNOWN",
        "FAILED",
    }
    assert set(schemas["DocumentVersion"]["properties"]["status"]["enum"]) == {
        "REGISTERED",
        "QUEUED",
        "PARSING",
        "INDEXING",
        "READY",
        "FAILED",
        "SUPERSEDED",
    }


def test_workflow_and_ticket_details_include_notification_summary(openapi: dict[str, Any]) -> None:
    schemas = openapi["components"]["schemas"]
    workflow_summary = schemas["WorkflowSummary"]
    ticket = schemas["Ticket"]

    assert "notification_summary" in workflow_summary["required"]
    assert workflow_summary["properties"]["notification_summary"]["$ref"].endswith(
        "/NotificationSummary"
    )
    assert "notification_summary" in ticket["required"]
    assert ticket["properties"]["notification_summary"]["$ref"].endswith("/NotificationSummary")


def test_problem_responses_follow_rfc_9457_media_contract(openapi: dict[str, Any]) -> None:
    components = openapi["components"]
    problem = components["schemas"]["Problem"]
    required_project_members = {"type", "title", "status", "code", "request_id"}

    assert required_project_members <= set(problem["required"])
    assert problem["properties"]["type"]["format"] == "uri-reference"
    assert problem["properties"]["status"]["minimum"] == 400
    assert problem["properties"]["status"]["maximum"] == 599
    assert "detail" in problem["properties"]

    for name, response in components["responses"].items():
        content = response.get("content", {})
        assert "application/problem+json" in content, (
            f"error response component {name} must use the RFC 9457 media type"
        )
        schema = content["application/problem+json"]["schema"]
        assert schema == {"$ref": "#/components/schemas/Problem"}


def test_common_event_envelope_example_and_required_fields() -> None:
    markdown = EVENTS_PATH.read_text(encoding="utf-8")
    common_envelope_section = markdown.split("## Common Envelope", maxsplit=1)[1]
    match = re.search(r"```json\s*(\{.*?\})\s*```", common_envelope_section, re.DOTALL)
    assert match, "Common Envelope must include one JSON example"
    envelope = json.loads(match.group(1))

    required = {
        "message_id",
        "event_type",
        "schema_version",
        "occurred_at",
        "producer",
        "aggregate",
        "operation_id",
        "trace",
        "payload",
    }
    assert required <= set(envelope)
    UUID(envelope["message_id"])
    assert re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_-]*)+", envelope["event_type"])
    assert isinstance(envelope["schema_version"], int) and envelope["schema_version"] > 0
    assert (
        datetime.fromisoformat(envelope["occurred_at"].replace("Z", "+00:00")).utcoffset()
        is not None
    )
    assert envelope["producer"]
    assert isinstance(envelope["payload"], dict)

    aggregate = envelope["aggregate"]
    assert {"type", "id", "version"} <= set(aggregate)
    UUID(aggregate["id"])
    assert isinstance(aggregate["version"], int) and aggregate["version"] > 0

    validation_section = common_envelope_section.split("### Envelope Validation", maxsplit=1)[1]
    for field in required:
        assert f"`{field}`" in validation_section, f"missing validation rule for {field}"


def test_event_contract_states_at_least_once_and_safe_unknown_schema_handling() -> None:
    markdown = EVENTS_PATH.read_text(encoding="utf-8")
    assert "Transport is at least once" in markdown
    assert "message_id" in markdown and "MUST remain unchanged" in markdown
    assert "Unsupported future major schema" in markdown
    assert "DLQ/quarantine" in markdown
