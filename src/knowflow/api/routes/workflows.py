"""Workflow create, list, get, message, and recovery endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from knowflow.api.dependencies import (
    CurrentAccessContext,
    get_request_id,
)
from knowflow.application.auth.policy import workflow_is_visible
from knowflow.application.workflows.service import (
    WorkflowCreateRequest,
    WorkflowService,
)

router = APIRouter(prefix="/workflows", tags=["Workflows"])


class CreateWorkflowBody(BaseModel):
    request_text: str
    operation_id: str


class WorkflowResponse(BaseModel):
    id: str
    thread_id: str
    status: str
    plan_id: str | None = None
    plan_version: int = 0
    deadline_at: str | None = None
    pending_approval_id: str | None = None
    version: int
    created_at: str
    updated_at: str


class WorkflowListResponse(BaseModel):
    items: list[WorkflowResponse]
    next_cursor: str | None = None


@router.post("", status_code=201, response_model=WorkflowResponse)
async def create_workflow(
    body: CreateWorkflowBody,
    context: CurrentAccessContext,
    request_id: Annotated[str, Depends(get_request_id)],
    request: Request,
) -> WorkflowResponse:
    service = _get_service(request)
    result = await service.accept_workflow(
        WorkflowCreateRequest(
            operation_id=body.operation_id,
            thread_id=request_id,
            request_text=body.request_text,
        ),
        lease_owner=request_id,
    )
    projection = await service.get_workflow(result.workflow_id)
    return WorkflowResponse(
        id=projection.id,
        thread_id=projection.thread_id,
        status=projection.status,
        plan_id=projection.plan_id,
        plan_version=projection.plan_version,
        deadline_at=projection.deadline_at.isoformat() if projection.deadline_at else None,
        pending_approval_id=projection.pending_approval_id,
        version=projection.version,
        created_at=projection.created_at.isoformat(),
        updated_at=projection.updated_at.isoformat(),
    )


@router.get("", response_model=WorkflowListResponse)
async def list_workflows(
    context: CurrentAccessContext,
    request: Request,
    status: Annotated[str | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> WorkflowListResponse:
    service = _get_service(request)
    items, next_cursor = await service.list_workflows(
        owner_user_id=context.user_id,
        status=status,
        cursor=cursor,
        limit=limit,
    )
    return WorkflowListResponse(
        items=[
            WorkflowResponse(
                id=wf.id,
                thread_id=wf.thread_id,
                status=wf.status,
                plan_id=wf.plan_id,
                plan_version=wf.plan_version,
                deadline_at=wf.deadline_at.isoformat() if wf.deadline_at else None,
                pending_approval_id=wf.pending_approval_id,
                version=wf.version,
                created_at=wf.created_at.isoformat(),
                updated_at=wf.updated_at.isoformat(),
            )
            for wf in items
        ],
        next_cursor=next_cursor,
    )


@router.get("/{workflow_id}", response_model=WorkflowResponse)
async def get_workflow(
    workflow_id: str,
    context: CurrentAccessContext,
    request: Request,
) -> WorkflowResponse:
    service = _get_service(request)
    projection = await service.get_workflow(workflow_id)
    if not workflow_is_visible(context, owner_user_id=projection.id):
        raise HTTPException(status_code=404, detail="Workflow not found")
    return WorkflowResponse(
        id=projection.id,
        thread_id=projection.thread_id,
        status=projection.status,
        plan_id=projection.plan_id,
        plan_version=projection.plan_version,
        deadline_at=projection.deadline_at.isoformat() if projection.deadline_at else None,
        pending_approval_id=projection.pending_approval_id,
        version=projection.version,
        created_at=projection.created_at.isoformat(),
        updated_at=projection.updated_at.isoformat(),
    )


def _get_service(request: Request) -> WorkflowService:
    return request.app.state.workflow_service


def register_workflow_routes(app: Any) -> None:
    app.include_router(router)


__all__ = [
    "register_workflow_routes",
    "router",
]
