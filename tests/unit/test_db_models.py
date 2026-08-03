"""Structural contract tests for the foundational SQLAlchemy model registry.

The assertions intentionally use ``Base.metadata`` so they validate what an
Alembic autogeneration run can actually see, not merely which ORM classes exist.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

import pytest
from sqlalchemy import CheckConstraint, Index, Table, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

import knowflow.infrastructure.db.models  # noqa: F401 - registers mapped tables
from knowflow.infrastructure.db.session import Base

MODEL_GROUPS = {
    "identity": {"users", "teams", "roles", "user_roles", "login_sessions"},
    "workflow": {
        "workflows",
        "workflow_plans",
        "plan_tasks",
        "plan_dependencies",
        "workflow_commands",
    },
    "ticketing_reliability": {
        "tickets",
        "ticket_events",
        "approvals",
        "operation_ledger",
        "outbox_events",
        "inbox_messages",
        "notification_deliveries",
    },
    "knowledge_audit_evaluation": {
        "documents",
        "document_acl_grants",
        "document_versions",
        "document_segments",
        "retrieval_evidence",
        "audit_events",
        "evaluation_runs",
        "evaluation_results",
    },
}


def _table(name: str) -> Table:
    assert name in Base.metadata.tables, f"model registry is missing table {name!r}"
    return Base.metadata.tables[name]


def _column_names(columns: Iterable[object]) -> tuple[str, ...]:
    return tuple(column.name for column in columns)  # type: ignore[attr-defined]


def _unique_column_sets(table: Table) -> set[tuple[str, ...]]:
    unique_sets = {
        _column_names(constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    if table.primary_key.columns:
        unique_sets.add(_column_names(table.primary_key.columns))
    unique_sets.update(_column_names(index.columns) for index in table.indexes if index.unique)
    unique_sets.update((column.name,) for column in table.columns if column.unique)
    return unique_sets


def _index_column_sets(table: Table) -> set[tuple[str, ...]]:
    return {_column_names(index.columns) for index in table.indexes if isinstance(index, Index)}


def _normalized_checks(table: Table) -> list[str]:
    return [
        re.sub(r"[`\s()]", "", str(constraint.sqltext)).lower()
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    ]


def _assert_columns(table_name: str, required: set[str]) -> None:
    table = _table(table_name)
    missing = required - set(table.columns.keys())
    assert not missing, f"{table_name} is missing required columns: {sorted(missing)}"


def _assert_unique(table_name: str, columns: tuple[str, ...]) -> None:
    uniques = _unique_column_sets(_table(table_name))
    assert columns in uniques, f"{table_name} must enforce UNIQUE{columns}; found {sorted(uniques)}"


def _assert_foreign_key(table_name: str, column_name: str, target: str) -> None:
    column = _table(table_name).c[column_name]
    targets = {foreign_key.target_fullname for foreign_key in column.foreign_keys}
    assert target in targets, f"{table_name}.{column_name} must reference {target}; found {targets}"


def _assert_minimum_check(table_name: str, column_name: str, minimum: int) -> None:
    checks = _normalized_checks(_table(table_name))
    accepted = {
        f"{column_name}>={minimum}",
        f"{minimum}<={column_name}",
        f"{column_name}>{minimum - 1}",
        f"{minimum - 1}<{column_name}",
    }
    assert any(any(expression in check for expression in accepted) for check in checks), (
        f"{table_name}.{column_name} must have a database CheckConstraint enforcing >= {minimum}; "
        f"found {checks}"
    )


def _enum_values(table_name: str, column_name: str) -> set[str]:
    table = _table(table_name)
    column = table.c[column_name]
    native_values = getattr(column.type, "enums", None)
    if native_values:
        return {str(value) for value in native_values}

    check_text = " ".join(_normalized_checks(table)).upper()
    return set(re.findall(r"[A-Z][A-Z0-9_]+", check_text))


@pytest.mark.parametrize(("group", "expected"), MODEL_GROUPS.items())
def test_all_four_foundational_model_groups_are_registered(group: str, expected: set[str]) -> None:
    missing = expected - set(Base.metadata.tables)
    assert not missing, f"{group} model group is incomplete: {sorted(missing)}"


def test_every_foundational_table_has_a_primary_key() -> None:
    expected_tables = set().union(*MODEL_GROUPS.values())
    for table_name in sorted(expected_tables):
        primary_key = _table(table_name).primary_key
        assert primary_key.columns, f"{table_name} must have a database primary key"


def test_identity_foreign_keys_and_uniqueness_are_enforced() -> None:
    _assert_unique("users", ("username",))
    _assert_unique("teams", ("code",))
    _assert_unique("roles", ("code",))
    _assert_unique("user_roles", ("user_id", "role_id"))
    _assert_foreign_key("users", "team_id", "teams.id")
    _assert_foreign_key("user_roles", "user_id", "users.id")
    _assert_foreign_key("user_roles", "role_id", "roles.id")
    _assert_foreign_key("login_sessions", "user_id", "users.id")
    _assert_minimum_check("users", "acl_version", 1)


def test_workflow_models_support_versioned_plans_and_serial_commands() -> None:
    _assert_columns(
        "workflows",
        {
            "id",
            "thread_id",
            "owner_user_id",
            "session_id",
            "status",
            "plan_version",
            "pending_approval_id",
            "active_run_id",
            "version",
        },
    )
    _assert_unique("workflows", ("thread_id",))
    _assert_foreign_key("workflows", "owner_user_id", "users.id")
    _assert_foreign_key("workflows", "session_id", "login_sessions.id")
    _assert_minimum_check("workflows", "plan_version", 0)
    _assert_minimum_check("workflows", "version", 1)

    _assert_unique("workflow_plans", ("workflow_id", "version"))
    _assert_foreign_key("workflow_plans", "workflow_id", "workflows.id")
    _assert_minimum_check("workflow_plans", "version", 1)
    _assert_minimum_check("workflow_plans", "schema_version", 1)

    _assert_unique("plan_tasks", ("plan_id", "plan_version", "id"))
    _assert_unique("plan_tasks", ("operation_id",))
    _assert_minimum_check("plan_tasks", "position", 0)

    _assert_unique("workflow_commands", ("workflow_id", "sequence"))
    _assert_foreign_key("workflow_commands", "workflow_id", "workflows.id")
    _assert_minimum_check("workflow_commands", "sequence", 1)


def test_workflow_and_ticket_have_optimistic_versions() -> None:
    for table_name in ("workflows", "tickets"):
        table = _table(table_name)
        version = table.c.version
        assert not version.nullable, f"{table_name}.version must be required for compare-and-swap"
        _assert_minimum_check(table_name, "version", 1)

    _assert_unique("tickets", ("key",))
    _assert_minimum_check("tickets", "sla_version", 0)
    _assert_minimum_check("tickets", "escalation_level", 0)


def test_approval_is_bound_to_the_exact_plan_operation_and_resource() -> None:
    required_binding = {
        "workflow_id",
        "plan_id",
        "plan_version",
        "task_id",
        "operation_id",
        "action_type",
        "resource_type",
        "resource_id",
        "resource_version",
        "payload_hash",
        "requester_user_id",
        "status",
        "expires_at",
        "version",
    }
    _assert_columns("approvals", required_binding)
    approval = _table("approvals")
    nullable_only = {"resource_version"}
    for column_name in required_binding - nullable_only:
        assert not approval.c[column_name].nullable, f"approvals.{column_name} must be required"
    _assert_foreign_key("approvals", "workflow_id", "workflows.id")
    _assert_foreign_key("approvals", "requester_user_id", "users.id")
    _assert_minimum_check("approvals", "plan_version", 1)
    _assert_minimum_check("approvals", "version", 1)
    assert _enum_values("approvals", "status") == {
        "PENDING",
        "APPROVED",
        "REJECTED",
        "EXPIRED",
        "INVALIDATED",
    }


def test_operation_outbox_and_inbox_have_durable_idempotency_keys() -> None:
    operation = _table("operation_ledger")
    assert _column_names(operation.primary_key.columns) == ("operation_id",)
    _assert_unique("operation_ledger", ("scope_type", "scope_id", "operation_type", "operation_id"))
    _assert_columns(
        "operation_ledger",
        {"payload_hash", "status", "attempt_count", "lease_owner", "lease_until"},
    )
    _assert_minimum_check("operation_ledger", "attempt_count", 0)

    _assert_unique("outbox_events", ("message_id",))
    _assert_columns(
        "outbox_events",
        {"event_type", "schema_version", "payload", "status", "attempt_count", "lease_until"},
    )
    _assert_minimum_check("outbox_events", "schema_version", 1)
    _assert_minimum_check("outbox_events", "attempt_count", 0)

    inbox = _table("inbox_messages")
    assert _column_names(inbox.primary_key.columns) == ("consumer_group", "message_id")
    _assert_columns("inbox_messages", {"payload_hash", "status", "attempt_count"})
    _assert_minimum_check("inbox_messages", "attempt_count", 0)

    _assert_unique("notification_deliveries", ("operation_id",))


def test_notification_delivery_uses_the_six_public_contract_states() -> None:
    assert _enum_values("notification_deliveries", "status") == {
        "PENDING",
        "SENDING",
        "DELIVERED",
        "RETRYING",
        "UNKNOWN",
        "FAILED",
    }


def test_document_versions_are_structurally_immutable_and_retry_diagnosable() -> None:
    required = {
        "id",
        "document_id",
        "version",
        "checksum",
        "media_type",
        "source_location",
        "parser_version",
        "chunker_version",
        "embedding_version",
        "status",
        "attempt_count",
        "failure_stage",
        "failure_code",
        "failure_summary",
        "created_at",
        "ready_at",
    }
    _assert_columns("document_versions", required)
    versions = _table("document_versions")
    _assert_unique("document_versions", ("document_id", "version"))
    _assert_foreign_key("document_versions", "document_id", "documents.id")
    _assert_minimum_check("document_versions", "version", 1)
    _assert_minimum_check("document_versions", "attempt_count", 1)
    for immutable_column in ("document_id", "version", "checksum", "source_location"):
        assert versions.c[immutable_column].onupdate is None
    assert _enum_values("document_versions", "status") == {
        "REGISTERED",
        "QUEUED",
        "PARSING",
        "INDEXING",
        "READY",
        "FAILED",
        "SUPERSEDED",
    }


def test_document_acl_segments_and_evaluation_results_are_uniquely_scoped() -> None:
    _assert_unique("document_acl_grants", ("document_id", "principal_type", "principal_id"))
    _assert_foreign_key("document_acl_grants", "document_id", "documents.id")
    _assert_unique("document_segments", ("document_version_id", "sequence"))
    _assert_foreign_key("document_segments", "document_version_id", "document_versions.id")
    _assert_unique("evaluation_results", ("run_id", "case_id"))
    _assert_foreign_key("evaluation_results", "run_id", "evaluation_runs.id")


def test_audit_events_have_resource_scoped_timeline_indexes() -> None:
    _assert_columns(
        "audit_events",
        {
            "id",
            "sequence",
            "occurred_at",
            "actor_user_id",
            "workflow_id",
            "ticket_id",
            "resource_type",
            "resource_id",
            "action",
            "outcome",
        },
    )
    indexes = _index_column_sets(_table("audit_events"))

    def has_ordered_scope(scope: str) -> bool:
        return any(
            columns[0] == scope and ("sequence" in columns[1:] or "occurred_at" in columns[1:])
            for columns in indexes
            if columns
        )

    assert has_ordered_scope("workflow_id"), (
        f"audit_events needs a workflow timeline index; found {sorted(indexes)}"
    )
    assert has_ordered_scope("ticket_id"), (
        f"audit_events needs a ticket timeline index; found {sorted(indexes)}"
    )
    assert any(columns[:2] == ("resource_type", "resource_id") for columns in indexes), (
        f"audit_events needs a resource lookup index; found {sorted(indexes)}"
    )


def test_mysql_ddl_quotes_reserved_rank_in_retrieval_check() -> None:
    ddl = str(CreateTable(_table("retrieval_evidence")).compile(dialect=mysql.dialect()))
    assert "CHECK (`rank` >= 1)" in ddl
