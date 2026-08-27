# KnowFlow Agent Platform — V2

KnowFlow 是一个面向单企业多用户场景的 **企业知识、工单与事故可靠执行 Agent 平台**。
V2 将旧版“知识 + 工单 Agent”和 Resolve 的“Incident 调查/处置”统一为一个执行模型：
自然语言先被解析为原子 Intent，再由确定性编译器生成跨通道 `IntentExecutionPlan` Task DAG；只有需要持续推理的任务进入唯一固定 Coordinator LangGraph。

> 当前仓库包含 2026-08-03 的 legacy baseline 规格和 2026-08-27 的 V2 amendment。V2 文档是当前推荐口径；旧文件保留用于历史追溯。

## V2 governing architecture

```text
User Query / Follow-up Turn / Incident Trigger
                  ↓
          Intent Recognition (LLM)
                  ↓
          Slot Validation (rules)
                  ↓
     IntentExecutionPlan / Task DAG
                  ↓
             PlanExecutor
        ┌─────────┼──────────┐
        ↓         ↓          ↓
 DIRECT_READ  BUSINESS   CONTROL_COMMAND
             ACTION
        └─────────┬──────────┘
                  ↓ when reasoning is needed
        Fixed Coordinator LangGraph
        Plan → Evidence/RAG → Diagnosis
        → Remediation? → Policy/Approval/Execute?
        → Verify? → Notify?
```

**Never:** `Intent -> Node -> dynamically build a LangGraph`.

**Always:** `Intent -> ExecutionPlan -> execution channel`.

## Four execution channels

| Channel | Examples | Runtime |
|---|---|---|
| `DIRECT_READ` | knowledge, metrics, logs, MQ lag, ticket/incident query | direct tool/query service; safe reads may run concurrently |
| `BUSINESS_ACTION` | ticket create/update, ordinary notification | deterministic application service |
| `CONTROL_COMMAND` | approve/reject/cancel/takeover | deterministic authorization/state command |
| `COORDINATOR` | investigate, remediation, rollback/restart/scale, verify | the single fixed Coordinator Graph |

## Six demo scenarios

1. `payment_release_incident` — complaints + error/P95 + MQ lag + rev-42 → two-round investigation → approval rollback → verify → support update.
2. `mq_backlog_remediation` — MQ backlog → evidence/Runbook → conditional consumer restart → verification.
3. `knowledge_query` — ACL/version-safe cited RAG; **no Agent Graph**.
4. `multi_intent_ticket` — search Runbook + P1 ticket + on-call notification + investigate + conditional restart; Task DAG crosses multiple channels.
5. `query_status` — direct ticket/incident status query; **no Agent Graph**.
6. `conversation_thread` — one Thread, multiple Turns and Runs, refinement/extension with `parent_run_id` lineage.

## Current V2 documents

- [V2 feature specification](specs/001-knowflow-agent-platform/spec-v2.md)
- [V2 implementation plan](specs/001-knowflow-agent-platform/plan-v2.md)
- [V2 data model amendment](specs/001-knowflow-agent-platform/data-model-v2.md)
- [V2 quickstart / scenario contract](specs/001-knowflow-agent-platform/quickstart-v2.md)
- [V2 task list](specs/001-knowflow-agent-platform/tasks-v2.md)
- [Execution architecture decision](docs/V2_EXECUTION_ARCHITECTURE.md)
- [Browseable V2 teaching demo](demo/README.md)

## Legacy baseline

The original files remain unchanged: `spec.md`, `plan.md`, `data-model.md`, `quickstart.md`, and `tasks.md` describe the initial two-week MVP baseline. V2 documents supersede them where terminology or execution architecture differs.

## Reliability statement

> Delivery is at least once. Stable message/operation identities, MySQL constraints, Outbox/Inbox, downstream idempotency ledgers, UNKNOWN reconciliation and fencing prevent duplicate business effects. Transport-level exactly-once is not claimed.
