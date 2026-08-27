# ADR: V2 IntentExecutionPlan and Fixed Coordinator Graph

**Decision date:** 2026-08-27  
**Status:** Accepted

## Decision

KnowFlow will not build LangGraph topology from detected user intents. The model produces a structured multi-intent recognition result. A deterministic compiler then creates a request-specific `IntentExecutionPlan` Task DAG. A deterministic PlanExecutor dispatches tasks by ExecutionChannel. Only tasks needing sustained reasoning enter the one predefined Coordinator Graph.

## Why

Dynamic Intent -> Node graph assembly mixes probabilistic interpretation with runtime authority, makes checkpoint/recovery semantics harder to reason about, complicates versioning, and encourages direct coupling between user wording and side effects.

A separate Task DAG provides the required flexibility (dependencies, parallelism, conditions) while keeping Agent orchestration fixed, auditable and recoverable.

## Consequences

- Pure reads avoid unnecessary Agent cost/latency.
- Ticket/application writes stay deterministic.
- Approval/cancel/takeover stay outside model authority.
- The Coordinator Graph can still stop at Diagnosis, Remediation, Execute, Verify or Notify according to data flags.
- Compound requests can expose atomic tasks while coalescing Coordinator tasks into one graph invocation.
- `Command(resume=...)` remains primarily an interrupt/resume mechanism.

## Key Interview Answer

> 多意图识别后我不会把每个 Intent 对应的 Node 临时拼成 LangGraph。模型只输出 Intent 和 slots，确定性 PlanCompiler 生成带依赖/条件的 IntentExecutionPlan。PlanExecutor 按 Direct Read、Business Action、Control Command 和 Coordinator 四类通道调度；只有持续推理任务进入唯一固定 Graph。IntentExecutionPlan 决定任务边界，Graph 的 conditional edges 决定固定拓扑里具体走到哪一阶段。
