# Feature Specification V2: Multi-Scenario KnowFlow Agent Platform

**Date**: 2026-08-27  
**Status**: Approved design / interview-learning implementation  
**Supersedes where conflicting**: `spec.md`

## Product Positioning

KnowFlow V2 merges two product surfaces into one platform:

1. direct enterprise knowledge/ticket tasks initiated by natural-language users;
2. Incident-oriented investigation and reliable remediation triggered by users or operational signals.

The platform is not a generic chatbot and not an autonomous multi-agent network. LangGraph remains the only main Agent orchestrator.

## Architectural Invariant: Intent Is Not Graph Topology

The request pipeline MUST be:

```text
Query/Turn -> IntentRecognitionResult -> SlotValidator -> IntentExecutionPlan -> PlanExecutor
                                                               ├─ DIRECT_READ
                                                               ├─ BUSINESS_ACTION
                                                               ├─ CONTROL_COMMAND
                                                               └─ COORDINATOR
```

The system MUST NOT construct a new LangGraph from detected intents. The Coordinator Graph MUST be predefined and versioned. Intent results are data consumed by the application layer and, when relevant, Graph State.

## IntentRecognitionResult

The model output contains:

- `schema_version`;
- one or more atomic `intents` (`code`, `confidence`, business `slots`, rationale);
- semantic `execution_order`;
- optional follow-up relation and slot updates.

The model MUST NOT decide identity, roles, scopes, risk, approval status, resource ownership, task authorization, or graph topology.

## Slot Clarification

A deterministic validator MUST check required business slots against the intent catalog plus trusted UI/Thread business context. A missing critical field MUST produce `NEEDS_CLARIFICATION`; no write task and no Coordinator Run may start until the missing field is valid.

## IntentExecutionPlan

The deterministic compiler MUST produce a versioned plan containing:

- atomic tasks with stable `task_id`;
- `ExecutionChannel`;
- validated slots;
- dependencies;
- optional condition;
- Coordinator directives for allowed stages/tools;
- `thread_id` and `turn_id`.

The Task DAG expresses business dependencies, not LangGraph node edges.

## Four Execution Channels

### DIRECT_READ

Examples: `SEARCH_KNOWLEDGE`, `QUERY_METRICS`, `SEARCH_LOGS`, `QUERY_DEPLOYMENT`, `QUERY_MQ_LAG`, `QUERY_TICKET`, `QUERY_INCIDENT`.

Independent safe reads SHOULD execute concurrently when dependencies allow. They do not start the Coordinator Graph unless the compound request also contains a reasoning task.

### BUSINESS_ACTION

Examples: `CREATE_TICKET`, `UPDATE_TICKET`, ordinary `SEND_NOTIFICATION`.

These MUST execute through deterministic application services with RBAC, object authorization, state-machine checks, stable idempotency identities, transactions and audit. A model never writes a Ticket directly.

### CONTROL_COMMAND

Examples: `APPROVE_OPERATION`, `REJECT_OPERATION`, `CANCEL_RUN`, `TAKEOVER_RUN`.

These MUST call deterministic approval/run-control modules using the authenticated principal. `Command(resume=...)` is an execution-framework resume mechanism, not the authority source.

### COORDINATOR

Examples: `INVESTIGATE_INCIDENT`, `REINVESTIGATE_INCIDENT`, `PROPOSE_REMEDIATION`, `ROLLBACK_DEPLOYMENT`, `RESTART_SERVICE`, `SCALE_WORKER`, `VERIFY_RECOVERY`.

All such work uses the same predefined Coordinator Graph. Multiple Coordinator intents in one compound request SHOULD normally be coalesced into one graph invocation and represented as Graph directives.

## Fixed Coordinator Graph

```text
normalize/correlate
 -> plan_investigation
 -> gather_evidence + retrieve_knowledge
 -> build_diagnosis
    -> evidence insufficient and budget remains: re-plan
    -> no remediation requested: END
 -> propose_remediation
    -> execution not requested: END
 -> policy_gate
 -> optional approval -> interrupt/resume -> preflight
 -> execute_operation
    -> UNKNOWN: status reconciliation
    -> verification not requested: END
 -> verify_recovery
    -> failed: compensate/escalate
    -> notification not requested: END
 -> notify -> END
```

Stage decisions MUST use fixed conditional edges / state values rather than runtime graph construction.

## Conditional Remediation

A request such as “if Consumer is abnormal, request restart” MUST compile the condition into the execution plan. The Coordinator MUST evaluate the condition against structured diagnosis facts before entering the remediation/write path. The LLM may propose diagnosis facts but does not authorize bypassing the condition.

## Thread / Turn / Run

- **Thread**: persistent business conversation context.
- **Turn**: immutable user/system message.
- **Run**: one concrete plan/Agent execution.

A Thread MAY contain many Runs. Follow-up Turns MUST create new plans and, when Agent work is required, new Runs with `parent_run_id` lineage; terminal Runs are not mutated back into execution.

The intent model SHOULD receive a compact Thread projection (summary, active entities, current Run, recent Turns), not an unbounded transcript.

## Required Demo Scenarios

### S1 Payment Release Incident

Complaint + error/P95 + MQ lag + rev-42 change -> Incident -> two-round evidence gathering -> cited diagnosis -> rollback proposal -> High risk Policy -> human approval -> stable Operation -> Verify -> support notification.

### S2 MQ Backlog Remediation

MQ backlog -> investigation/knowledge -> Consumer abnormal condition -> restart proposal -> Policy/Approval/Operation -> lag verification.

### S3 Knowledge Query

Permission/version-safe knowledge retrieval with citations; Coordinator Graph not started.

### S4 Compound Knowledge/Ticket/Notification/Investigation

Input: “查 MQ 积压处理手册，创建 P1 工单通知值班人员，如果确认 Consumer 异常就申请重启。”

Required atomic plan:

```text
SEARCH_KNOWLEDGE
  ├─ CREATE_TICKET -> SEND_NOTIFICATION
  └─ INVESTIGATE_INCIDENT -> RESTART_SERVICE if consumer_abnormal
```

Ticket and investigation may run in parallel after required knowledge is available. The investigation/restart tasks belong to one Coordinator group and do not imply multiple graph topologies.

### S5 Query Status

Direct Ticket/Incident query, no Agent Graph.

### S6 Persistent Conversation Thread

At least four Turns in one Thread demonstrating goal refinement, investigation extension and a later remediation request; each Agent turn produces a new Run with lineage.

## Reliability Requirements Retained from Baseline

- MySQL is the authority for business truth; Redis checkpoint state is recoverable coordination state.
- Stable `operation_id` for every side effect.
- Operation payload identity mismatch is rejected.
- Outbox is committed with producer business state; Inbox completion is committed with consumer local effect.
- At-least-once delivery is expected and visible.
- UNKNOWN external result is reconciled by stable identity/status query before retry.
- Fencing prevents a stale worker from executing after lease takeover.
- Approval authorizes one exact snapshot and is invalidated by material drift.
- Execute success does not resolve an Incident; independent Verify is required when requested.
