# V2 Implementation Tasks

## Phase A — Request Understanding and Plan Compilation

- [x] V2-001 Define explicit atomic `IntentCode` catalog and interview-safe source boundary.
- [x] V2-002 Define `IntentRecognitionResult`, follow-up relation and recognized slot types.
- [x] V2-003 Implement deterministic required-slot validation and NEEDS_CLARIFICATION plan.
- [x] V2-004 Define four `ExecutionChannel` values and route mapping.
- [x] V2-005 Implement deterministic `PlanCompiler` producing `IntentExecutionPlan` Task DAG.
- [x] V2-006 Add Coordinator directives and structured conditional write constraints.

## Phase B — Cross-Channel Runtime

- [x] V2-010 Implement deterministic `PlanExecutor`.
- [x] V2-011 Support concurrent ready DIRECT_READ tasks.
- [x] V2-012 Implement Ticket create/query/update teaching service with idempotency/optimistic version semantics.
- [x] V2-013 Implement deterministic Business Action executor.
- [x] V2-014 Implement Approval/Cancel/Takeover Control Command executor.
- [x] V2-015 Coalesce tasks sharing a coordinator group into one fixed Graph invocation.

## Phase C — Conversation

- [x] V2-020 Define Thread / immutable Turn / Run lineage models.
- [x] V2-021 Build compact Thread Context projection.
- [x] V2-022 Support `REFINE_EXISTING_GOAL` and `EXTEND_EXISTING_INVESTIGATION` intent relations.
- [x] V2-023 Create new Runs for follow-up Agent work rather than mutating terminal history.

## Phase D — Fixed Coordinator Graph

- [x] V2-030 Remove intent-classifier and slot-clarification nodes from the Coordinator Graph.
- [x] V2-031 Add `execution_plan` / `coordinator_directives` to compact Graph State.
- [x] V2-032 Route Diagnosis -> END or Remediation by directives.
- [x] V2-033 Route Remediation -> END or Policy by execution request.
- [x] V2-034 Route Execute -> END / reconcile / Verify by stage request and outcome.
- [x] V2-035 Route Verify -> END / Notify / escalation.
- [x] V2-036 Evaluate conditional writes after structured diagnosis facts.
- [x] V2-037 Preserve Approval interrupt/resume, Preflight, Operation, UNKNOWN reconciliation and Verify semantics.

## Phase E — Six Scenarios

- [x] V2-040 Payment release incident.
- [x] V2-041 MQ backlog remediation.
- [x] V2-042 Pure knowledge query.
- [x] V2-043 Compound knowledge/ticket/notify/investigate/conditional restart.
- [x] V2-044 Direct ticket/incident status query.
- [x] V2-045 Persistent conversation thread.
- [x] V2-046 Add `demo_runner.py` scenario registry/trace output.

## Phase F — Documentation and Static Evidence

- [x] V2-050 Update V2 README/architecture/interview explanations.
- [x] V2-051 Preserve source mapping between baseline spec, Resolve spec and educational extensions.
- [ ] V2-052 Publish the complete multi-file learning Demo artifact on the V2 branch.
- [ ] V2-053 Publish regenerated V2 interview textbook artifact.
