"""Self-contained V2 architecture reference used by the GitHub browseable demo.

This is intentionally smaller than the complete learning package. It demonstrates the architectural
contract that matters for the V2 spec: multi-intent recognition is data, PlanCompiler creates a Task
DAG, and LangGraph is a fixed execution channel rather than dynamically assembled per request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Channel(StrEnum):
    DIRECT_READ = "DIRECT_READ"
    BUSINESS_ACTION = "BUSINESS_ACTION"
    COORDINATOR = "COORDINATOR"
    CONTROL_COMMAND = "CONTROL_COMMAND"


DEFAULT_CHANNEL = {
    "SEARCH_KNOWLEDGE": Channel.DIRECT_READ,
    "QUERY_METRICS": Channel.DIRECT_READ,
    "SEARCH_LOGS": Channel.DIRECT_READ,
    "QUERY_MQ_LAG": Channel.DIRECT_READ,
    "QUERY_TICKET": Channel.DIRECT_READ,
    "QUERY_INCIDENT": Channel.DIRECT_READ,
    "CREATE_TICKET": Channel.BUSINESS_ACTION,
    "UPDATE_TICKET": Channel.BUSINESS_ACTION,
    "SEND_NOTIFICATION": Channel.BUSINESS_ACTION,
    "APPROVE_OPERATION": Channel.CONTROL_COMMAND,
    "REJECT_OPERATION": Channel.CONTROL_COMMAND,
    "CANCEL_RUN": Channel.CONTROL_COMMAND,
    "TAKEOVER_RUN": Channel.CONTROL_COMMAND,
    "INVESTIGATE_INCIDENT": Channel.COORDINATOR,
    "PROPOSE_REMEDIATION": Channel.COORDINATOR,
    "ROLLBACK_DEPLOYMENT": Channel.COORDINATOR,
    "RESTART_SERVICE": Channel.COORDINATOR,
    "VERIFY_RECOVERY": Channel.COORDINATOR,
}


@dataclass
class Intent:
    code: str
    slots: dict = field(default_factory=dict)


@dataclass
class Task:
    task_id: str
    intent: str
    channel: Channel
    depends_on: list[str] = field(default_factory=list)
    condition: str | None = None


@dataclass
class ExecutionPlan:
    tasks: list[Task]
    directives: dict


def compile_multi_intent_ticket() -> ExecutionPlan:
    """Compile the flagship compound request into a business Task DAG."""

    tasks = [
        Task("T1", "SEARCH_KNOWLEDGE", Channel.DIRECT_READ),
        Task("T2", "CREATE_TICKET", Channel.BUSINESS_ACTION, ["T1"]),
        Task("T3", "SEND_NOTIFICATION", Channel.BUSINESS_ACTION, ["T2"]),
        Task("T4", "INVESTIGATE_INCIDENT", Channel.COORDINATOR, ["T1"]),
        Task("T5", "RESTART_SERVICE", Channel.COORDINATOR, ["T4"], "consumer_abnormal == true"),
    ]
    directives = {
        "broad_investigation": True,
        "remediation_requested": True,
        "execution_requested": True,
        "requested_write_tools": ["ops.restart_service"],
        "write_conditions": {"ops.restart_service": "consumer_abnormal == true"},
        "coordinator_group": "coordinator-main",
    }
    return ExecutionPlan(tasks=tasks, directives=directives)


def render_scenario(name: str) -> str:
    """Return a compact human-readable architecture trace."""

    if name == "multi_intent_ticket":
        plan = compile_multi_intent_ticket()
        rows = [f"{t.task_id} {t.intent} [{t.channel}] deps={t.depends_on} condition={t.condition}" for t in plan.tasks]
        return "\n".join([
            "Query -> IntentRecognitionResult -> SlotValidator -> IntentExecutionPlan",
            *rows,
            f"Coordinator directives: {plan.directives}",
            "T4/T5 are atomic plan tasks but one fixed Coordinator Graph invocation.",
        ])
    return f"{name}: see specs/001-knowflow-agent-platform/quickstart-v2.md for the full scenario contract"


def list_scenarios() -> list[str]:
    """Return the six V2 scenario names."""

    return [
        "payment_release_incident",
        "mq_backlog_remediation",
        "knowledge_query",
        "multi_intent_ticket",
        "query_status",
        "conversation_thread",
    ]
