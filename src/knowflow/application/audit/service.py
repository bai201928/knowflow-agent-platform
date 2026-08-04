from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowflow.application.auth.policy import (
    AccessContext,
    Capability,
    require_capability,
)
from knowflow.infrastructure.db.models.identity import AuditEvent


@dataclass(frozen=True, slots=True)
class AuditEntry:
    actor_user_id: str | None = None
    actor_session_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    correlation_id: str | None = None
    workflow_id: str | None = None
    ticket_id: str | None = None
    plan_id: str | None = None
    plan_version: int | None = None
    task_id: str | None = None
    operation_id: str | None = None
    message_id: str | None = None
    action: str = ""
    resource_type: str = ""
    resource_id: str | None = None
    authorization_decision: str = "ALLOW"
    outcome: str = "SUCCEEDED"
    reason_code: str | None = None
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AuditTimelineQuery:
    workflow_id: str | None = None
    ticket_id: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    cursor: str | None = None
    limit: int = 50


@dataclass(frozen=True, slots=True)
class AuditTimelineResult:
    items: list[dict[str, Any]]
    next_cursor: str | None = None


SessionFactory = Callable[[], AsyncSession]
Clock = Callable[[], datetime]


class AuditService:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def write_audit(self, entry: AuditEntry) -> dict[str, Any]:
        if not entry.action.strip():
            raise ValueError("action is required")
        if not entry.resource_type.strip():
            raise ValueError("resource_type is required")
        redacted = _redact_metadata(entry.metadata)
        now = self._now()
        async with self._session_factory() as session:
            audit = AuditEvent(
                occurred_at=now,
                actor_user_id=entry.actor_user_id,
                actor_session_id=entry.actor_session_id,
                request_id=entry.request_id,
                trace_id=entry.trace_id,
                correlation_id=entry.correlation_id,
                workflow_id=entry.workflow_id,
                ticket_id=entry.ticket_id,
                plan_id=entry.plan_id,
                plan_version=entry.plan_version,
                task_id=entry.task_id,
                operation_id=entry.operation_id,
                message_id=entry.message_id,
                action=entry.action,
                resource_type=entry.resource_type,
                resource_id=entry.resource_id,
                authorization_decision=entry.authorization_decision,
                outcome=entry.outcome,
                reason_code=entry.reason_code,
                reason=entry.reason,
            )
            audit.metadata = redacted  # type: ignore[assignment]
            session.add(audit)
            await session.flush()
            return {
                "id": audit.id,
                "sequence": audit.sequence,
                "occurred_at": audit.occurred_at.isoformat().replace(
                    "+00:00", "Z"
                ),
                "action": audit.action,
                "resource_type": audit.resource_type,
                "resource_id": audit.resource_id,
                "outcome": audit.outcome,
            }

    async def read_timeline(
        self,
        query: AuditTimelineQuery,
        *,
        context: AccessContext | None = None,
    ) -> AuditTimelineResult:
        if query.limit < 1 or query.limit > 200:
            raise ValueError("limit must be between 1 and 200")
        async with self._session_factory() as session:
            stmt = select(AuditEvent).order_by(
                AuditEvent.sequence.desc()
            )
            if query.workflow_id:
                stmt = stmt.where(
                    AuditEvent.workflow_id == query.workflow_id
                )
            if query.ticket_id:
                stmt = stmt.where(
                    AuditEvent.ticket_id == query.ticket_id
                )
            if query.resource_type and query.resource_id:
                stmt = stmt.where(
                    AuditEvent.resource_type == query.resource_type,
                    AuditEvent.resource_id == query.resource_id,
                )
            if query.cursor:
                try:
                    cursor_seq = int(query.cursor)
                    stmt = stmt.where(
                        AuditEvent.sequence < cursor_seq
                    )
                except (TypeError, ValueError) as err:
                    raise ValueError(
                        "cursor must be a valid sequence number"
                    ) from err
            stmt = stmt.limit(query.limit + 1)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            has_more = len(rows) > query.limit
            items = rows[:query.limit]
            timeline: list[dict[str, Any]] = []
            for audit in items:
                item: dict[str, Any] = {
                    "id": audit.id,
                    "sequence": audit.sequence,
                    "occurred_at": audit.occurred_at.isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "action": audit.action,
                    "resource_type": audit.resource_type,
                    "resource_id": audit.resource_id,
                    "outcome": audit.outcome,
                    "reason_code": audit.reason_code,
                    "reason": audit.reason,
                }
                if context is not None:
                    try:
                        require_capability(
                            context, Capability.AUDIT_READ
                        )
                        item["actor_user_id"] = audit.actor_user_id
                    except PermissionError:
                        if audit.actor_user_id == context.user_id:
                            item["actor_user_id"] = audit.actor_user_id
                        else:
                            item["actor_user_id"] = "[REDACTED]"
                else:
                    item["actor_user_id"] = audit.actor_user_id
                timeline.append(item)
            next_cursor = (
                str(items[-1].sequence)
                if has_more and items
                else None
            )
            return AuditTimelineResult(
                items=timeline, next_cursor=next_cursor
            )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError(
                "audit clock must return timezone-aware datetime"
            )
        return now.astimezone(UTC)


_SENSITIVE_KEYS = frozenset({
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "passwd",
    "secret",
    "token",
    "bearer",
    "access_key",
    "private_key",
})


def _redact_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    if not metadata:
        return {}
    result: dict[str, Any] = {}
    for key, value in metadata.items():
        if any(s in key.lower() for s in _SENSITIVE_KEYS):
            result[key] = "[REDACTED]"
        elif isinstance(value, str):
            lowered = value.lower()
            if any(
                s in lowered
                for s in (
                    "bearer ",
                    "api_key=",
                    "password=",
                    "secret=",
                    "token=",
                )
            ):
                result[key] = "[REDACTED]"
            elif len(value) > 500:
                result[key] = value[:500]
            else:
                result[key] = value
        elif isinstance(value, dict):
            result[key] = _redact_metadata(value)
        elif isinstance(value, list):
            result[key] = [_redact_value(v) for v in value[:50]]
        else:
            result[key] = value
    return result


def _redact_value(value: Any) -> Any:
    if isinstance(value, str) and len(value) > 500:
        return value[:500]
    if isinstance(value, dict):
        return _redact_metadata(value)
    return value


__all__ = [
    "AuditEntry",
    "AuditService",
    "AuditTimelineQuery",
    "AuditTimelineResult",
]
