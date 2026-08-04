"""LangGraph assembly for the flagship incident workflow.

Routes are conditional on the workflow status and pending barriers (clarification,
approval).  The graph is intentionally assembled with dependency-injected services
so the same structure can use real or stub adapters.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from knowflow.workflows.nodes.incident import (
    approval_node,
    approval_resume_node,
    clarification_node,
    error_handler_node,
    final_summary_node,
    notification_node,
    planner_node,
    retrieval_node,
    sandbox_node,
    ticket_node,
)
from knowflow.workflows.state import WorkflowState


def _has_clarification(state: WorkflowState) -> bool:
    return state.get("pending_clarification") is not None


def _has_pending_approval(state: WorkflowState) -> bool:
    approval = state.get("pending_approval")
    return approval is not None


def _is_approved(state: WorkflowState) -> bool:
    approval = state.get("pending_approval")
    if approval is None:
        return False
    return state.get("status") == "RUNNING"


def build_incident_graph(
    *,
    planner: Any,
    compiler: Any,
    retrieval_service: Any,
    ticket_service: Any,
    notification_service: Any,
    approval_service: Any,
    sandbox_executor: Any,
    audit_service: Any,
    access_context: Any,
    lease_owner: str,
    clock: Callable[[], datetime] | None = None,
) -> Any:
    """Assemble and return the compiled LangGraph for incident resolution.

    The returned graph is ready for `.ainvoke()` or `.astream()` with a
    :class:`WorkflowState` initial value.  The caller is responsible for
    wiring a Redis-backed checkpointer before invocation.

    Args:
        planner: Application planner service.
        compiler: Application plan compiler.
        retrieval_service: Knowledge retrieval port.
        ticket_service: Idempotent ticket creation service.
        notification_service: Durable notification registration.
        approval_service: Approval lifecycle service.
        sandbox_executor: Sandbox operation executor.
        audit_service: Append-only audit service.
        access_context: Auth context for the current request.
        lease_owner: Stable identifier for this graph run.
        clock: Timezone-aware UTC clock; defaults to system clock.

    Returns:
        A compiled LangGraph `StateGraph`.
    """
    from langgraph.graph import END, START, StateGraph

    _clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))

    workflow = StateGraph(WorkflowState)

    # ------------------------------------------------------------------
    # Node factories - each creates a closure capturing services.
    # ------------------------------------------------------------------
    async def _planner(state: WorkflowState) -> dict[str, Any]:
        return await planner_node(
            state,
            planner=planner,
            compiler=compiler,
            clock=_clock,
            lease_owner=lease_owner,
        )

    async def _clarification(state: WorkflowState) -> dict[str, Any]:
        return await clarification_node(state)

    async def _retrieval(state: WorkflowState) -> dict[str, Any]:
        return await retrieval_node(
            state,
            retrieval_service=retrieval_service,
            access_context=access_context,
        )

    async def _ticket(state: WorkflowState) -> dict[str, Any]:
        return await ticket_node(
            state,
            ticket_service=ticket_service,
            access_context=access_context,
            lease_owner=lease_owner,
        )

    async def _notification(state: WorkflowState) -> dict[str, Any]:
        return await notification_node(
            state,
            notification_service=notification_service,
            access_context=access_context,
            lease_owner=lease_owner,
        )

    async def _approval(state: WorkflowState) -> dict[str, Any]:
        return await approval_node(
            state,
            approval_service=approval_service,
            access_context=access_context,
            lease_owner=lease_owner,
            clock=_clock,
        )

    async def _approval_resume(state: WorkflowState) -> dict[str, Any]:
        return await approval_resume_node(
            state,
            clock=_clock,
        )

    async def _sandbox(state: WorkflowState) -> dict[str, Any]:
        return await sandbox_node(
            state,
            sandbox_executor=sandbox_executor,
            lease_owner=lease_owner,
        )

    async def _final_summary(state: WorkflowState) -> dict[str, Any]:
        return await final_summary_node(
            state,
            audit_service=audit_service,
        )

    async def _error_handler(state: WorkflowState) -> dict[str, Any]:
        return await error_handler_node(
            state,
            clock=_clock,
        )

    # ------------------------------------------------------------------
    # Register nodes.
    # ------------------------------------------------------------------
    workflow.add_node("planner", _planner)
    workflow.add_node("clarification", _clarification)
    workflow.add_node("retrieval", _retrieval)
    workflow.add_node("ticket", _ticket)
    workflow.add_node("notification", _notification)
    workflow.add_node("approval", _approval)
    workflow.add_node("approval_resume", _approval_resume)
    workflow.add_node("sandbox", _sandbox)
    workflow.add_node("final_summary", _final_summary)
    workflow.add_node("error_handler", _error_handler)

    # ------------------------------------------------------------------
    # Routing functions.
    # ------------------------------------------------------------------
    def route_after_planner(
        state: WorkflowState,
    ) -> Literal["clarification", "retrieval", "__end__"]:
        if state.get("status") == "FAILED":
            return "__end__"
        if _has_clarification(state):
            return "clarification"
        return "retrieval"

    def route_after_retrieval(
        state: WorkflowState,
    ) -> Literal["ticket", "__end__"]:
        if state.get("status") == "FAILED":
            return "__end__"
        return "ticket"

    def route_after_ticket(
        state: WorkflowState,
    ) -> Literal["notification", "approval", "final_summary", "__end__"]:
        if state.get("status") in ("FAILED", "CANCELLED"):
            return "__end__"
        if _has_pending_approval(state):
            return "approval"
        return "notification"

    def route_after_notification(
        state: WorkflowState,
    ) -> Literal["approval", "sandbox", "final_summary", "__end__"]:
        if state.get("status") in ("FAILED", "CANCELLED"):
            return "__end__"
        if _has_pending_approval(state):
            return "approval"
        return "sandbox"

    def route_after_approval(
        state: WorkflowState,
    ) -> Literal["approval_resume", "sandbox", "final_summary", "__end__"]:
        if state.get("status") in ("FAILED", "CANCELLED"):
            return "__end__"
        if state.get("status") == "WAITING_APPROVAL":
            return "__end__"
        if _is_approved(state):
            return "approval_resume"
        return "final_summary"

    # ------------------------------------------------------------------
    # Wire edges.
    # ------------------------------------------------------------------
    workflow.add_edge(START, "planner")

    workflow.add_conditional_edges(
        "planner",
        route_after_planner,
        {
            "clarification": "clarification",
            "retrieval": "retrieval",
            "__end__": END,
        },
    )

    workflow.add_edge("clarification", "planner")

    workflow.add_conditional_edges(
        "retrieval",
        route_after_retrieval,
        {
            "ticket": "ticket",
            "__end__": END,
        },
    )

    workflow.add_conditional_edges(
        "ticket",
        route_after_ticket,
        {
            "notification": "notification",
            "approval": "approval",
            "final_summary": "final_summary",
            "__end__": END,
        },
    )

    workflow.add_conditional_edges(
        "notification",
        route_after_notification,
        {
            "approval": "approval",
            "sandbox": "sandbox",
            "final_summary": "final_summary",
            "__end__": END,
        },
    )

    workflow.add_conditional_edges(
        "approval",
        route_after_approval,
        {
            "approval_resume": "approval_resume",
            "sandbox": "sandbox",
            "final_summary": "final_summary",
            "__end__": END,
        },
    )

    workflow.add_edge("approval_resume", "sandbox")

    workflow.add_edge("sandbox", "final_summary")
    workflow.add_edge("final_summary", END)
    workflow.add_edge("error_handler", END)

    return workflow.compile()


__all__ = [
    "build_incident_graph",
    "get_incident_graph",
]

_graph_singleton: Any | None = None


def get_incident_graph(**deps: Any) -> Any:
    """Return the compiled incident graph singleton.

    Builds the graph on first call with the provided dependencies.
    Subsequent calls return the cached compiled graph.
    """
    global _graph_singleton
    if _graph_singleton is None:
        _graph_singleton = build_incident_graph(**deps)
    return _graph_singleton
