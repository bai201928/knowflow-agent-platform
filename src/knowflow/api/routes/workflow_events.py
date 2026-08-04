"""SSE streaming for workflow events with authorization and reconnect."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from knowflow.api.dependencies import CurrentAccessContext

router = APIRouter(prefix="/workflows", tags=["Workflow Events"])


@router.get("/{workflow_id}/events")
async def stream_workflow_events(
    workflow_id: str,
    context: CurrentAccessContext,
    request: Request,
) -> StreamingResponse:
    async def event_stream() -> Any:
        yield f"event: connected\ndata: {{\"workflow_id\": \"{workflow_id}\"}}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                yield ": keepalive\n\n"
                await asyncio.sleep(15)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def register_sse_routes(app: Any) -> None:
    app.include_router(router)


__all__ = [
    "register_sse_routes",
    "router",
]
