"""Untrusted model planning boundary for workflow requests.

The planner is intentionally limited to recalling candidate business intents and
requesting a schema-validated plan candidate.  It does not resolve actors,
capabilities, resources, tools, task identities, or operation identities; those
decisions belong to the deterministic compiler and server-owned catalog.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ValidationError

from knowflow.domain.workflows.catalog import INTENT_CATALOG_VERSION
from knowflow.domain.workflows.schemas import (
    CandidateDependency,
    DependencyKind,
    IntentType,
    KnowledgeQueryIntent,
    ModelPlanCandidate,
    NotificationSendIntent,
    OpsActionIntent,
    SlotDataType,
    TicketCreateIntent,
    UntrustedSlotCandidate,
)
from knowflow.infrastructure.models.adapters import (
    ChatMessage,
    ChatModelPort,
    ModelAdapterError,
    ProviderErrorKind,
)

PLANNER_PROMPT_VERSION: Final = "planner-v1"


class PlannerErrorKind(StrEnum):
    """Sanitized planner failures exposed to orchestration code."""

    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    TIMEOUT = "TIMEOUT"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    INVALID_OUTPUT = "INVALID_OUTPUT"


class PlannerError(Exception):
    """A payload-free planner failure safe for API and audit translation."""

    def __init__(self, kind: PlannerErrorKind, *, retryable: bool) -> None:
        self.kind = kind
        self.retryable = retryable
        super().__init__(f"Workflow planner error: {kind.value}")


@dataclass(frozen=True, slots=True)
class PlannerResult:
    """Model candidate plus the immutable evidence needed by the compiler."""

    candidate: ModelPlanCandidate
    source_model: str
    prompt_version: str
    catalog_version: str
    candidate_intents: tuple[IntentType, ...]


_RECALL_PATTERNS: Final[dict[IntentType, tuple[re.Pattern[str], ...]]] = {
    IntentType.KNOWLEDGE_QUERY: (
        re.compile(r"\b(?:diagnos(?:e|is)|investigat(?:e|ion)|search|find|lookup|explain)\b"),
        re.compile(r"(?:诊断|排查|检索|搜索|查找|知识查询|原因分析)"),
    ),
    IntentType.TICKET_CREATE: (
        re.compile(
            r"\b(?:create|open|raise|file)\b.{0,40}\b(?:ticket|incident)\b"
            r"|\b(?:ticket|incident)\b.{0,40}\b(?:create|open|raise|file)\b"
        ),
        re.compile(r"(?:创建|新建|提交|发起).{0,20}(?:工单|事件|故障单)"),
    ),
    IntentType.TICKET_QUERY: (
        re.compile(
            r"\b(?:get|query|view|show|check|list)\b.{0,40}\b(?:ticket|incident)s?\b"
            r"|\b(?:ticket|incident)\s+(?:status|details?)\b"
        ),
        re.compile(r"(?:查询|查看|列出).{0,20}(?:工单|事件|故障单)"),
    ),
    IntentType.TICKET_UPDATE: (
        re.compile(
            r"\b(?:update|assign|close|resolve|reopen|comment)\b.{0,40}"
            r"\b(?:ticket|incident)\b"
            r"|\b(?:ticket|incident)\b.{0,40}"
            r"\b(?:update|assign|close|resolve|reopen|comment)\b"
        ),
        re.compile(r"(?:更新|指派|关闭|解决|重开|评论).{0,20}(?:工单|事件|故障单)"),
    ),
    IntentType.NOTIFICATION_SEND: (
        re.compile(r"\b(?:notify|notification|alert|message|email)\b"),
        re.compile(r"(?:通知|告警|提醒|发送消息|发邮件)"),
    ),
    IntentType.OPS_ACTION: (
        re.compile(r"\b(?:restart|rollback|deploy|scale|failover|remediate)\b"),
        re.compile(r"(?:重启|回滚|发布|扩容|缩容|故障转移|执行运维)"),
    ),
}


def recall_candidate_intents(request_text: str) -> tuple[IntentType, ...]:
    """Return every catalog intent, prioritizing lexical candidates by first mention.

    Recall is deliberately high-recall and non-authoritative: unmatched intents stay
    in the tail so this helper can never grant or deny a capability.  The fixed
    server catalog and compiler make the eventual authorization decision.
    """

    normalized = request_text.casefold()
    catalog_order = {intent: index for index, intent in enumerate(IntentType)}
    first_mentions: dict[IntentType, int] = {}
    for intent, patterns in _RECALL_PATTERNS.items():
        positions = [match.start() for pattern in patterns if (match := pattern.search(normalized))]
        if positions:
            first_mentions[intent] = min(positions)

    return tuple(
        sorted(
            IntentType,
            key=lambda intent: (
                intent not in first_mentions,
                first_mentions.get(intent, 0),
                catalog_order[intent],
            ),
        )
    )


class WorkflowPlanner:
    """Create an untrusted structured candidate through a provider-neutral port."""

    def __init__(
        self,
        *,
        model: ChatModelPort,
        source_model: str,
        prompt_version: str = PLANNER_PROMPT_VERSION,
    ) -> None:
        self._model = model
        self._source_model = _normalized_label(source_model, field="source_model", maximum=128)
        self._prompt_version = _normalized_label(
            prompt_version,
            field="prompt_version",
            maximum=64,
        )

    async def plan(self, *, request_text: str, deadline: float) -> PlannerResult:
        """Ask the configured model for a strict candidate and retain version evidence."""

        if deadline <= time.monotonic():
            raise PlannerError(PlannerErrorKind.DEADLINE_EXCEEDED, retryable=False)
        recalled = recall_candidate_intents(request_text)
        messages = self._messages(request_text=request_text, candidate_intents=recalled)

        candidate: ModelPlanCandidate | None = None
        normalized_error: PlannerError | None = None
        try:
            raw_candidate = await self._model.chat_structured(
                messages=messages,
                response_model=ModelPlanCandidate,
                deadline=deadline,
            )
            # Revalidate a serialized boundary even when a custom adapter claims to
            # return the requested Pydantic type.  Adapter objects remain untrusted.
            payload = _model_payload(raw_candidate)
            candidate = ModelPlanCandidate.model_validate(payload)
        except ModelAdapterError as error:
            normalized_error = _normalize_provider_error(error)
        except (AttributeError, TypeError, ValueError, ValidationError):
            normalized_error = PlannerError(PlannerErrorKind.INVALID_OUTPUT, retryable=False)
        except Exception:
            normalized_error = PlannerError(PlannerErrorKind.DEPENDENCY_FAILURE, retryable=False)

        if normalized_error is not None:
            # Raise outside the handling block so no provider exception or payload
            # remains reachable through exception chaining.
            normalized_error.__cause__ = None
            normalized_error.__context__ = None
            raise normalized_error
        if candidate is None:
            raise PlannerError(PlannerErrorKind.INVALID_OUTPUT, retryable=False)

        return PlannerResult(
            candidate=candidate,
            source_model=self._source_model,
            prompt_version=self._prompt_version,
            catalog_version=INTENT_CATALOG_VERSION,
            candidate_intents=recalled,
        )

    def _messages(
        self,
        *,
        request_text: str,
        candidate_intents: tuple[IntentType, ...],
    ) -> tuple[ChatMessage, ChatMessage]:
        candidates = ", ".join(intent.value for intent in candidate_intents)
        system_prompt = (
            "Produce only a ModelPlanCandidate JSON object. "
            f"Prompt version: {self._prompt_version}. "
            f"Intent catalog version: {INTENT_CATALOG_VERSION}. "
            f"Candidate intents in recall order: {candidates}. "
            "Use only catalog intent names and request-derived slot candidates. "
            "All output is untrusted: never assert actor identity, authorization, "
            "resource trust, task identity, operation identity, capability selection, "
            "or execution routing. Missing required values must be listed as missing slots."
        )
        return (
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=request_text),
        )


def stub_plan_fixture(name: str) -> ModelPlanCandidate:
    """Return a fresh deterministic offline candidate for a named scenario."""

    factory = _STUB_FIXTURES.get(name)
    if factory is None:
        raise ValueError("unknown deterministic planner fixture")
    return factory()


def _flagship_incident_fixture() -> ModelPlanCandidate:
    return ModelPlanCandidate(
        schema_version=1,
        tasks=(
            KnowledgeQueryIntent(
                task_key="diagnose",
                source_span="diagnose the RocketMQ backlog",
                slots=(
                    UntrustedSlotCandidate(
                        name="query",
                        value_type=SlotDataType.STRING,
                        value="RocketMQ consumer backlog",
                        source_span="RocketMQ consumer backlog",
                    ),
                ),
                confidence=0.98,
            ),
            TicketCreateIntent(
                task_key="create_ticket",
                source_span="create a P1 ticket",
                slots=(
                    UntrustedSlotCandidate(
                        name="title",
                        value_type=SlotDataType.STRING,
                        value="RocketMQ consumer backlog",
                        source_span="RocketMQ consumer backlog",
                    ),
                    UntrustedSlotCandidate(
                        name="description",
                        value_type=SlotDataType.STRING,
                        value="Backlog remains above the incident threshold",
                        source_span="Backlog remains above the incident threshold",
                    ),
                    UntrustedSlotCandidate(
                        name="severity",
                        value_type=SlotDataType.STRING,
                        value="P1",
                        source_span="P1",
                    ),
                ),
                confidence=0.97,
            ),
            NotificationSendIntent(
                task_key="notify",
                source_span="notify the RocketMQ on-call group",
                slots=(
                    UntrustedSlotCandidate(
                        name="recipient_scope",
                        value_type=SlotDataType.STRING,
                        value="team:rocketmq-oncall",
                        source_span="team:rocketmq-oncall",
                    ),
                    UntrustedSlotCandidate(
                        name="template_key",
                        value_type=SlotDataType.STRING,
                        value="incident.p1.created",
                        source_span="incident.p1.created",
                    ),
                ),
                confidence=0.95,
            ),
            OpsActionIntent(
                task_key="restart",
                source_span="restart the RocketMQ consumer after approval",
                slots=(
                    UntrustedSlotCandidate(
                        name="consumer_group",
                        value_type=SlotDataType.STRING,
                        value="payments-consumer",
                        source_span="payments-consumer",
                    ),
                    UntrustedSlotCandidate(
                        name="reason",
                        value_type=SlotDataType.STRING,
                        value="clear sustained backlog",
                        source_span="clear sustained backlog",
                    ),
                ),
                confidence=0.93,
            ),
        ),
        dependencies=(
            CandidateDependency(
                from_task_key="diagnose",
                to_task_key="create_ticket",
                kind=DependencyKind.SEQUENCE,
            ),
            CandidateDependency(
                from_task_key="create_ticket",
                to_task_key="notify",
                kind=DependencyKind.DATA,
                output_binding={"ticket_id": "ticket_id"},
            ),
            CandidateDependency(
                from_task_key="notify",
                to_task_key="restart",
                kind=DependencyKind.SEQUENCE,
            ),
        ),
    )


_STUB_FIXTURES: Final = {"flagship_incident": _flagship_incident_fixture}


def _normalized_label(value: str, *, field: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field} must be between 1 and {maximum} characters")
    return normalized


def _model_payload(candidate: BaseModel) -> object:
    return candidate.model_dump(mode="json")


def _normalize_provider_error(error: ModelAdapterError) -> PlannerError:
    if error.kind is ProviderErrorKind.DEADLINE_EXCEEDED:
        return PlannerError(PlannerErrorKind.DEADLINE_EXCEEDED, retryable=error.retryable)
    if error.kind is ProviderErrorKind.TIMEOUT:
        return PlannerError(PlannerErrorKind.TIMEOUT, retryable=error.retryable)
    if error.kind is ProviderErrorKind.INVALID_RESPONSE:
        return PlannerError(PlannerErrorKind.INVALID_OUTPUT, retryable=False)
    return PlannerError(PlannerErrorKind.DEPENDENCY_FAILURE, retryable=error.retryable)


__all__ = [
    "PLANNER_PROMPT_VERSION",
    "PlannerError",
    "PlannerErrorKind",
    "PlannerResult",
    "WorkflowPlanner",
    "recall_candidate_intents",
    "stub_plan_fixture",
]
