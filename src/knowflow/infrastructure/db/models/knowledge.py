"""Knowledge, citation-evidence, and evaluation mappings."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from knowflow.infrastructure.db.session import Base, UTCDateTime, utc_now


def _uuid() -> str:
    return str(uuid4())


def _enum(enum_type: type[StrEnum], name: str) -> Enum:
    return Enum(
        enum_type,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        length=max(len(item.value) for item in enum_type),
    )


class DocumentVisibility(StrEnum):
    PUBLIC = "PUBLIC"
    ROLE = "ROLE"
    TEAM = "TEAM"
    EXPLICIT = "EXPLICIT"


class DocumentStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ACLPrincipalType(StrEnum):
    ROLE = "ROLE"
    TEAM = "TEAM"
    USER = "USER"


class DocumentVersionStatus(StrEnum):
    REGISTERED = "REGISTERED"
    QUEUED = "QUEUED"
    PARSING = "PARSING"
    INDEXING = "INDEXING"
    READY = "READY"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"


class EvaluationSuite(StrEnum):
    INTENT = "INTENT"
    RAG = "RAG"
    WORKFLOW = "WORKFLOW"
    FAULT = "FAULT"
    LOAD = "LOAD"
    REAL_MODEL = "REAL_MODEL"


class EvaluationRunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["active_version_id"],
            ["document_versions.id"],
            name="fk_documents_active_version",
            use_alter=True,
            ondelete="SET NULL",
        ),
        CheckConstraint("char_length(title) BETWEEN 1 AND 300", name="document_title_length"),
        Index("ix_documents_scope_status", "visibility_mode", "owner_team_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    source_location: Mapped[str] = mapped_column(String(1000), nullable=False)
    owner_team_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    visibility_mode: Mapped[DocumentVisibility] = mapped_column(
        _enum(DocumentVisibility, "document_visibility"), nullable=False
    )
    active_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[DocumentStatus] = mapped_column(
        _enum(DocumentStatus, "document_status"),
        nullable=False,
        default=DocumentStatus.ACTIVE,
    )
    created_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )


class DocumentACLGrant(Base):
    __tablename__ = "document_acl_grants"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "principal_type",
            "principal_id",
            name="uq_document_acl_principal",
        ),
        Index("ix_document_acl_principal", "principal_type", "principal_id"),
    )

    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    principal_type: Mapped[ACLPrincipalType] = mapped_column(
        _enum(ACLPrincipalType, "document_acl_principal_type"), primary_key=True
    )
    principal_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    granted_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    granted_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version", name="uq_document_versions_document_version"),
        CheckConstraint("version >= 1", name="document_version_positive"),
        CheckConstraint("attempt_count >= 1", name="document_version_attempt_positive"),
        CheckConstraint("page_count IS NULL OR page_count >= 0", name="document_page_nonnegative"),
        CheckConstraint(
            "segment_count IS NULL OR segment_count >= 0", name="document_segment_nonnegative"
        ),
        CheckConstraint(
            "token_estimate IS NULL OR token_estimate >= 0", name="document_token_nonnegative"
        ),
        Index("ix_document_versions_status", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    source_location: Mapped[str] = mapped_column(String(1000), nullable=False)
    normalized_text: Mapped[str | None] = mapped_column(LONGTEXT, nullable=True)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    chunker_version: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_version: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[DocumentVersionStatus] = mapped_column(
        _enum(DocumentVersionStatus, "document_version_status"),
        nullable=False,
        default=DocumentVersionStatus.REGISTERED,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    failure_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    segment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    registered_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    queued_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    parsing_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    indexing_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    ready_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now
    )


class DocumentSegment(Base):
    __tablename__ = "document_segments"
    __table_args__ = (
        UniqueConstraint(
            "document_version_id", "sequence", name="uq_document_segments_version_sequence"
        ),
        CheckConstraint("sequence >= 0", name="document_segment_sequence_nonnegative"),
        CheckConstraint("token_count >= 0", name="document_segment_tokens_nonnegative"),
        Index("ix_document_segments_active", "document_version_id", "active"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    section_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_anchor: Mapped[str | None] = mapped_column(String(500), nullable=True)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class RetrievalEvidence(Base):
    __tablename__ = "retrieval_evidence"
    __table_args__ = (
        CheckConstraint("`rank` >= 1", name="retrieval_rank_positive"),
        Index("ix_retrieval_evidence_workflow", "workflow_id", "task_id", "created_at"),
        Index("ix_retrieval_evidence_segment", "segment_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    workflow_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[str] = mapped_column(String(36), nullable=False)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False)
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    redacted_query: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    user_scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    acl_version: Mapped[int] = mapped_column(Integer, nullable=False)
    retrieval_config_version: Mapped[str] = mapped_column(String(128), nullable=False)
    segment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document_segments.id", ondelete="RESTRICT"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    dense_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    bm25_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    fused_score: Mapped[float] = mapped_column(Float, nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    __table_args__ = (
        CheckConstraint("sample_count >= 0", name="evaluation_sample_nonnegative"),
        CheckConstraint("error_count >= 0", name="evaluation_error_nonnegative"),
        Index("ix_evaluation_runs_suite_started", "suite", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    suite: Mapped[EvaluationSuite] = mapped_column(
        _enum(EvaluationSuite, "evaluation_suite"), nullable=False
    )
    dataset: Mapped[str] = mapped_column(String(255), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(128), nullable=False)
    code_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    adapter_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    retrieval_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    environment: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    status: Mapped[EvaluationRunStatus] = mapped_column(
        _enum(EvaluationRunStatus, "evaluation_run_status"),
        nullable=False,
        default=EvaluationRunStatus.PENDING,
    )
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    report_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class EvaluationResult(Base):
    __tablename__ = "evaluation_results"

    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("evaluation_runs.id", ondelete="CASCADE"), primary_key=True
    )
    case_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    expected_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    actual_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    workflow_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True
    )
    artifact_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
