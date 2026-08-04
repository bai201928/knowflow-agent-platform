"""Approval list, get, and decision endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from knowflow.api.dependencies import CurrentAccessContext

router = APIRouter(prefix="/approvals", tags=["Approvals"])


class ApprovalDecisionBody(BaseModel):
    decision: str
    reason: str


class ApprovalResponse(BaseModel):
    id: str
    workflow_id: str
    status: str
    action_type: str
    resource_type: str
    resource_id: str
    decided_at: str | None = None
    decision_reason: str | None = None
    expires_at: str
    created_at: str


class ApprovalListResponse(BaseModel):
    items: list[ApprovalResponse]
    next_cursor: str | None = None


@router.get("", response_model=ApprovalListResponse)
async def list_approvals(
    context: CurrentAccessContext,
    request: Request,
) -> ApprovalListResponse:
    return ApprovalListResponse(items=[])


@router.get("/{approval_id}", response_model=ApprovalResponse)
async def get_approval(
    approval_id: str,
    context: CurrentAccessContext,
    request: Request,
) -> ApprovalResponse:
    raise HTTPException(status_code=501, detail="Not yet implemented")


@router.post("/{approval_id}/decide", response_model=ApprovalResponse)
async def decide_approval(
    approval_id: str,
    body: ApprovalDecisionBody,
    context: CurrentAccessContext,
    request: Request,
) -> ApprovalResponse:
    raise HTTPException(status_code=501, detail="Not yet implemented")


def register_approval_routes(app: Any) -> None:
    app.include_router(router)


__all__ = [
    "register_approval_routes",
    "router",
]
