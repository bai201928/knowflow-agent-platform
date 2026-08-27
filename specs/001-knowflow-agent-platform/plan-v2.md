# Implementation Plan V2: IntentExecutionPlan + Multi-Scenario Demo

**Date**: 2026-08-27  
**Spec**: `spec-v2.md`

## Goal

Upgrade the interview demo from one flagship request to six independent scenarios while preserving the existing reliability core. Add a deterministic cross-channel Task DAG and persistent Thread semantics without introducing a second Agent runtime or expanding the tool catalog.

## Architecture

```text
IntentPlanningService
  -> LLM IntentRecognitionResult
  -> SlotValidator
  -> PlanCompiler
  -> IntentExecutionPlan
  -> PlanExecutor
       -> DirectReadExecutor
       -> BusinessActionExecutor
       -> ControlCommandExecutor
       -> one fixed Coordinator Graph
```

## Explicit Non-Goals

- no dynamic graph assembly;
- no second Agent framework;
- no new Kubernetes/GitHub/tool integrations for V2;
- no additional chaos scenarios beyond the existing reliability demo;
- no evaluation/observability dashboard expansion;
- no requirement to make the educational Demo production runnable.

## Components

### Intent layer

`IntentCode` is an explicit educational catalog. `IntentRecognitionResult` is model output. `SlotValidator` and `PlanCompiler` are deterministic.

### Plan layer

`IntentExecutionPlan` contains atomic tasks, dependencies, conditions, channels and Coordinator directives. Plan identity is versioned/auditable.

### Execution layer

`PlanExecutor` schedules ready tasks; independent Direct Reads may run concurrently. Deterministic business actions and control commands do not enter LangGraph.

Tasks sharing a `coordinator_group` are represented atomically in the Task DAG but coalesced into one Coordinator Graph invocation.

### Conversation layer

Persist Thread, append-only Turn and Run lineage. Build a compact context projection for follow-up intent parsing.

### Coordinator graph

Remove intent classification and slot clarification from Graph nodes. Add stage-aware conditional exits based on directives. Retain two-round investigation, Approval interrupt/resume, Operation, UNKNOWN reconciliation and Verify.

## Delivery Sequence

1. Define V2 intent/plan schemas and channel mapping.
2. Implement slot validation and plan compiler.
3. Implement deterministic plan executor and Ticket/query application services.
4. Add Thread/Turn/Run context model.
5. Refactor Coordinator Graph to consume directives only.
6. Add six scenario definitions and `demo_runner.py`.
7. Update README/interview documentation.
8. Preserve existing reliability failure demo.
9. Validate syntax, intent mapping, scenario registration and ZIP integrity.

## Acceptance

- Exactly one fixed Coordinator Graph definition exists.
- A pure knowledge query produces no Coordinator task.
- A ticket status query produces no Coordinator task.
- Full payment request compiles to one Coordinator group with execution/verification/notification directives.
- Compound MQ request exposes knowledge/ticket/notify/investigate/restart atomic tasks and cross-channel dependencies.
- Conditional restart is represented as `consumer_abnormal == true` and checked after diagnosis.
- Conversation scenario shows one Thread with at least four Turns and new Run lineage.
- Existing stable Operation, UNKNOWN, Outbox/Inbox, fencing and checkpoint-loss code remains present.
