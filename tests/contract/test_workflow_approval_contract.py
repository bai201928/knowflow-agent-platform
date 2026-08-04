"""Executable REST/SSE contracts for the flagship workflow and approval slice.

The checked-in OpenAPI 0.2.0 document is the public source of truth.  Static
assertions protect its exact request/response surface, while the TestClient
assertions intentionally stay red until the corresponding FastAPI routes are
registered.  Runtime checks stop at validation or concealed-not-found
boundaries, so they never need MySQL, Redis, RocketMQ, or a model provider.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
OPENAPI_PATH = ROOT / "specs" / "001-knowflow-agent-platform" / "contracts" / "openapi.yaml"
WORKFLOW_ID = "00000000-0000-4000-8000-000000000101"
APPROVAL_ID = "00000000-0000-4000-8000-000000000201"
VALID_IDEMPOTENCY_KEY = "contract-test-key-0001"
PROBLEM_REQUIRED = {"type", "title", "status", "code", "request_id"}

OperationExpectation = tuple[str, str, str, str, int]

OPERATIONS: tuple[OperationExpectation, ...] = (
    ("/workflows", "post", "createWorkflow", "WorkflowAccepted", 202),
    ("/workflows", "get", "listWorkflows", "WorkflowPage", 200),
    ("/workflows/{workflowId}", "get", "getWorkflow", "WorkflowDetail", 200),
    (
        "/workflows/{workflowId}/messages",
        "post",
        "appendWorkflowMessage",
        "WorkflowAccepted",
        202,
    ),
    (
        "/workflows/{workflowId}/cancel",
        "post",
        "cancelWorkflow",
        "WorkflowAccepted",
        202,
    ),
    (
        "/workflows/{workflowId}/events",
        "get",
        "streamWorkflowEvents",
        "WorkflowStreamEvent",
        200,
    ),
    (
        "/workflows/{workflowId}/audit-events",
        "get",
        "listWorkflowAuditEvents",
        "AuditEventPage",
        200,
    ),
    (
        "/workflows/{workflowId}/recovery-review",
        "get",
        "getWorkflowRecoveryReview",
        "RecoveryReview",
        200,
    ),
    (
        "/workflows/{workflowId}/recovery-decisions",
        "post",
        "decideWorkflowRecovery",
        "WorkflowAccepted",
        202,
    ),
    ("/approvals", "get", "listApprovals", "ApprovalPage", 200),
    ("/approvals/{approvalId}", "get", "getApproval", "Approval", 200),
    (
        "/approvals/{approvalId}/decision",
        "post",
        "decideApproval",
        "Approval",
        200,
    ),
)


@pytest.fixture(scope="module")
def published_openapi() -> dict[str, Any]:
    document = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    assert document["info"]["version"] == "0.2.0"
    return document


def _operation(document: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    assert path in document["paths"], f"missing public path: {path}"
    operation = document["paths"][path].get(method)
    assert isinstance(operation, dict), f"missing public operation: {method.upper()} {path}"
    return operation


def _local_schema_name(schema: dict[str, Any]) -> str:
    reference = schema.get("$ref")
    assert isinstance(reference, str) and reference.startswith("#/components/schemas/")
    return reference.rsplit("/", maxsplit=1)[-1]


def _walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _assert_problem(response: Any, expected_status: int) -> None:
    assert response.status_code == expected_status
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert PROBLEM_REQUIRED <= set(body)
    assert body["status"] == expected_status
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_published_operations_have_exact_success_response_contracts(
    published_openapi: dict[str, Any],
) -> None:
    for path, method, operation_id, response_schema, success_status in OPERATIONS:
        operation = _operation(published_openapi, path, method)
        assert operation["operationId"] == operation_id
        response = operation["responses"][str(success_status)]
        media_type = (
            "text/event-stream" if operation_id == "streamWorkflowEvents" else "application/json"
        )
        schema = response["content"][media_type]["schema"]
        if operation_id == "streamWorkflowEvents":
            assert schema == {"type": "string"}
            assert response_schema in response["description"]
        else:
            assert _local_schema_name(schema) == response_schema


def test_runtime_registers_the_published_workflow_and_approval_surface(app: FastAPI) -> None:
    with TestClient(app) as client:
        runtime_openapi = client.get("/openapi.json").json()

    for path, method, operation_id, _, _ in OPERATIONS:
        operation = _operation(runtime_openapi, path, method)
        assert operation["operationId"] == operation_id


def test_mutation_requests_are_closed_and_versioned(published_openapi: dict[str, Any]) -> None:
    schemas = published_openapi["components"]["schemas"]
    expectations = {
        "CreateWorkflowRequest": {"required": {"message"}, "closed": True},
        "WorkflowMessageRequest": {
            "required": {"content", "expected_version"},
            "closed": True,
        },
        "CancelWorkflowRequest": {
            "required": {"expected_version", "reason"},
            "closed": True,
        },
        "RecoveryDecisionRequest": {
            "required": {"action", "expected_workflow_version", "reason"},
            "closed": True,
        },
        "ApprovalDecisionRequest": {
            "required": {"decision", "expected_version"},
            "closed": True,
        },
    }
    for name, expectation in expectations.items():
        schema = schemas[name]
        assert set(schema["required"]) == expectation["required"]
        assert schema["additionalProperties"] is False

    assert schemas["WorkflowMessageRequest"]["properties"]["expected_version"]["minimum"] == 1
    assert schemas["CancelWorkflowRequest"]["properties"]["expected_version"]["minimum"] == 1
    assert (
        schemas["RecoveryDecisionRequest"]["properties"]["expected_workflow_version"]["minimum"]
        == 1
    )
    assert schemas["ApprovalDecisionRequest"]["properties"]["expected_version"]["minimum"] == 1


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/workflows", {"message": "Investigate the consumer backlog"}),
        (
            f"/workflows/{WORKFLOW_ID}/messages",
            {"content": "The consumer group is billing", "expected_version": 1},
        ),
        (
            f"/workflows/{WORKFLOW_ID}/cancel",
            {"expected_version": 1, "reason": "The incident is resolved"},
        ),
        (
            f"/workflows/{WORKFLOW_ID}/recovery-decisions",
            {
                "action": "RESUME_FROM_FACTS",
                "expected_workflow_version": 1,
                "reason": "Durable facts prove this resume is safe",
            },
        ),
        (
            f"/approvals/{APPROVAL_ID}/decision",
            {"decision": "APPROVE", "expected_version": 1, "reason": "Verified"},
        ),
    ],
)
def test_runtime_mutations_require_idempotency_key_as_problem_json(
    app: FastAPI,
    path: str,
    body: dict[str, Any],
) -> None:
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(path, json=body, headers={"X-Request-ID": "missing-idem-key"})
    _assert_problem(response, 422)


def test_all_business_mutations_publish_a_required_idempotency_header(
    published_openapi: dict[str, Any],
) -> None:
    required_operations = (
        ("/workflows", "post"),
        ("/workflows/{workflowId}/messages", "post"),
        ("/workflows/{workflowId}/cancel", "post"),
        ("/workflows/{workflowId}/recovery-decisions", "post"),
        ("/approvals/{approvalId}/decision", "post"),
    )
    idempotency_ref = "#/components/parameters/IdempotencyKey"
    for path, method in required_operations:
        references = {
            parameter.get("$ref")
            for parameter in _operation(published_openapi, path, method)["parameters"]
        }
        assert idempotency_ref in references

    parameter = published_openapi["components"]["parameters"]["IdempotencyKey"]
    assert parameter["in"] == "header"
    assert parameter["required"] is True
    assert parameter["schema"]["minLength"] == 16
    assert parameter["schema"]["maxLength"] == 128


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/workflows", {"message": ""}),
        (
            f"/workflows/{WORKFLOW_ID}/messages",
            {"content": "clarification", "expected_version": 0},
        ),
        (
            f"/workflows/{WORKFLOW_ID}/cancel",
            {"expected_version": 0, "reason": ""},
        ),
        (
            f"/workflows/{WORKFLOW_ID}/recovery-decisions",
            {
                "action": "FORCE_SUCCESS",
                "expected_workflow_version": 0,
                "reason": "",
            },
        ),
        (
            f"/approvals/{APPROVAL_ID}/decision",
            {"decision": "SKIP", "expected_version": 0},
        ),
    ],
)
def test_runtime_rejects_invalid_mutations_as_problem_json(
    app: FastAPI,
    path: str,
    body: dict[str, Any],
) -> None:
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            path,
            json=body,
            headers={
                "Idempotency-Key": VALID_IDEMPOTENCY_KEY,
                "X-Request-ID": "invalid-business-request",
            },
        )
    _assert_problem(response, 422)


def test_recovery_actions_are_explicit_and_cannot_bypass_review(
    published_openapi: dict[str, Any],
) -> None:
    schemas = published_openapi["components"]["schemas"]
    assert set(schemas["RecoveryAction"]["enum"]) == {
        "RESUME_FROM_FACTS",
        "RETRY_SAFE_STEP",
        "MARK_FAILED",
        "REQUIRE_NEW_APPROVAL",
    }
    review = schemas["RecoveryReview"]
    assert {
        "workflow_id",
        "workflow_version",
        "risk_level",
        "reason_code",
        "durable_facts",
        "permitted_actions",
    } <= set(review["required"])
    assert review["properties"]["permitted_actions"]["items"] == {
        "$ref": "#/components/schemas/RecoveryAction"
    }


def test_sse_contract_supports_durable_reconnect_and_named_events(
    published_openapi: dict[str, Any],
) -> None:
    operation = _operation(published_openapi, "/workflows/{workflowId}/events", "get")
    last_event_id = next(
        parameter
        for parameter in operation["parameters"]
        if parameter.get("name") == "Last-Event-ID"
    )
    assert last_event_id["in"] == "header"
    assert last_event_id["required"] is False
    assert "durable sequence" in operation["responses"]["200"]["description"]

    event_schema = published_openapi["components"]["schemas"]["WorkflowStreamEvent"]
    assert {"sequence", "workflow_id", "event", "occurred_at", "data"} <= set(
        event_schema["required"]
    )
    assert {"workflow.completed", "workflow.failed", "approval.required"} <= set(
        event_schema["properties"]["event"]["enum"]
    )


def test_runtime_sse_conceals_an_unknown_or_inaccessible_workflow(app: FastAPI) -> None:
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            f"/workflows/{WORKFLOW_ID}/events",
            headers={"Last-Event-ID": "41", "X-Request-ID": "sse-concealment"},
        )
    _assert_problem(response, 404)


@pytest.mark.parametrize(
    "path",
    [
        f"/workflows/{WORKFLOW_ID}",
        f"/workflows/{WORKFLOW_ID}/audit-events",
        f"/approvals/{APPROVAL_ID}",
    ],
)
def test_runtime_object_reads_use_concealed_not_found_problem_json(
    app: FastAPI,
    path: str,
) -> None:
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(path, headers={"X-Request-ID": "object-concealment"})
    _assert_problem(response, 404)


def test_openapi_defines_one_non_disclosing_not_found_problem(
    published_openapi: dict[str, Any],
) -> None:
    not_found = published_openapi["components"]["responses"]["NotFound"]
    assert "concealed" in not_found["description"]
    schema = not_found["content"]["application/problem+json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/Problem"}

    concealed_reads = (
        ("/workflows/{workflowId}", "get"),
        ("/workflows/{workflowId}/events", "get"),
        ("/workflows/{workflowId}/audit-events", "get"),
        ("/workflows/{workflowId}/recovery-review", "get"),
        ("/approvals/{approvalId}", "get"),
    )
    for path, method in concealed_reads:
        response = _operation(published_openapi, path, method)["responses"]["404"]
        assert response == {"$ref": "#/components/responses/NotFound"}


def test_workflow_reads_expose_notification_summary(published_openapi: dict[str, Any]) -> None:
    schemas = published_openapi["components"]["schemas"]
    workflow_summary = schemas["WorkflowSummary"]
    assert "notification_summary" in workflow_summary["required"]
    assert workflow_summary["properties"]["notification_summary"] == {
        "$ref": "#/components/schemas/NotificationSummary"
    }

    summary = schemas["NotificationSummary"]
    assert set(summary["required"]) == {
        "total",
        "pending",
        "delivered",
        "retrying",
        "unknown",
        "failed",
    }
    workflow_detail_refs = {
        node["$ref"]
        for node in _walk(schemas["WorkflowDetail"])
        if isinstance(node, dict) and "$ref" in node
    }
    assert "#/components/schemas/WorkflowSummary" in workflow_detail_refs


@pytest.mark.parametrize(
    "path",
    [
        "/workflows?limit=0",
        "/workflows?status=NOT_A_WORKFLOW_STATUS",
        "/approvals?limit=101",
        "/approvals?status=NOT_AN_APPROVAL_STATUS",
    ],
)
def test_runtime_lists_reject_invalid_filters_as_problem_json(app: FastAPI, path: str) -> None:
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(path, headers={"X-Request-ID": "invalid-list-filter"})
    _assert_problem(response, 422)
