# Data Model V2 Amendment

This document adds the V2 execution-plan and conversation entities. Baseline business entities in `data-model.md` remain valid.

## Conversation Thread

| Field | Meaning |
|---|---|
| `thread_id` | persistent conversation/business context identity |
| `org_id`, `owner_user_id` | authorization anchors |
| `summary` | bounded structured summary |
| `active_incident_id`, `active_ticket_id`, `active_service`, `environment` | active business entities |
| `current_run_id` | current Run pointer |
| `structured_context` | bounded versioned context facts |

Thread does not own business side effects and is not a substitute for Incident/Ticket/Operation truth.

## Conversation Turn

Append-only: `turn_id`, `thread_id`, role, redacted content, metadata, timestamp. Previous Turns are never rewritten to simulate a changed user request.

## Run Record

`run_id`, `thread_id`, `turn_id`, optional `parent_run_id`, `execution_plan_id`, status, graph/prompt/model/tool/retrieval version snapshots and result summary.

One Thread may own multiple Runs. Terminal Runs are not resumed as new work; follow-up creates a new Run with lineage.

## Intent Recognition Result

This is model output/trace evidence rather than trusted business authority:

- schema version;
- atomic intent code;
- confidence;
- extracted business slots;
- semantic execution order;
- follow-up relation and slot updates.

It MUST NOT contain trusted actor/authorization/approval facts.

## Intent Execution Plan

| Field | Meaning |
|---|---|
| `plan_id`, version | stable plan identity/version |
| `thread_id`, `turn_id` | conversation binding |
| intents | immutable recognition snapshot/reference |
| tasks | atomic execution tasks |
| coordinator_directives | one fixed Graph's allowed stages/capabilities |
| `missing_by_intent` | deterministic clarification result |

## Execution Task

| Field | Meaning |
|---|---|
| `task_id` | stable within plan version |
| `intent_code` | business goal represented by the task |
| `channel` | DIRECT_READ / BUSINESS_ACTION / COORDINATOR / CONTROL_COMMAND |
| `slots` | canonical validated business parameters |
| `depends_on` | task IDs |
| `condition` | bounded structured condition |
| `coordinator_group` | tasks coalesced into one fixed Graph invocation |
| `status`, result ref | durable task progress/projection |

The Task DAG MUST be acyclic. It is not a LangGraph topology.

## Coordinator Directives

- `broad_investigation`;
- `requested_read_tools`;
- `requested_rag_queries`;
- `remediation_requested`;
- `execution_requested`;
- `verification_requested`;
- `notification_requested`;
- `requested_write_tools`;
- `write_conditions`;
- `stop_after`.

These fields constrain the fixed Coordinator Graph. They do not authorize an operation; Policy/Approval remains authoritative.

## Existing Reliability Entities

`Approval`, `Operation Record`, `Outbox Event`, `Inbox Message`, `Notification Delivery`, Document/Version/Segment and Audit entities from the baseline remain unchanged in authority. A V2 task that produces a side effect MUST map to one stable Operation identity across retry/replay.
