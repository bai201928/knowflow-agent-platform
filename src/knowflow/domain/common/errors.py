from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    RECOVERY_REVIEW_REQUIRED = "RECOVERY_REVIEW_REQUIRED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


DEFAULT_STATUS: dict[ErrorCode, int] = {
    ErrorCode.AUTHENTICATION_REQUIRED: 401,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.DEADLINE_EXCEEDED: 504,
    ErrorCode.CAPACITY_EXCEEDED: 429,
    ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
    ErrorCode.APPROVAL_REQUIRED: 409,
    ErrorCode.RECOVERY_REVIEW_REQUIRED: 409,
    ErrorCode.INTERNAL_ERROR: 500,
}


class KnowFlowError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        detail: str | None = None,
        *,
        status: int | None = None,
        retryable: bool = False,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail or code.value)
        self.code = code
        self.detail = detail
        self.status = status or DEFAULT_STATUS[code]
        self.retryable = retryable
        self.context = context or {}

    def to_problem(
        self,
        *,
        request_id: str,
        current_version: int | None = None,
        fields: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        problem: dict[str, Any] = {
            "type": f"urn:knowflow:error:{self.code.value.lower().replace('_', '-')}",
            "title": self.code.value.replace("_", " ").title(),
            "status": self.status,
            "detail": self.detail,
            "code": self.code.value,
            "request_id": request_id,
            "retryable": self.retryable,
        }
        if current_version is not None:
            problem["current_version"] = current_version
        if fields:
            problem["fields"] = fields
        return problem


def concealed_not_found(detail: str | None = None) -> KnowFlowError:
    return KnowFlowError(ErrorCode.RESOURCE_NOT_FOUND, detail or "Resource not found", status=404)
