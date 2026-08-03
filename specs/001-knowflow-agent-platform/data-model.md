# Data Model: KnowFlow Reliable Agent Platform

## Modeling Rules

- MySQL is authoritative for every user-visible business fact and recovery decision.
- All timestamps are UTC and generated or validated server-side.
- Internal IDs use UUID strings at API/domain boundaries and compact binary UUID storage where useful.
- Human-facing tickets also have a unique monotonic display key such as `INC-000123`.
- Mutable aggregates carry an integer `version`; writes use `WHERE version = expected_version`.
- JSON values carry an explicit schema/version field and are validated before persistence or use.
- Secrets, full access tokens, and raw credentials are never persisted in these entities.
- Milvus, Redis, and RocketMQ records reference stable MySQL IDs and can be rebuilt or reconciled.

## Identity and Access

### User (`users`)

| Field | Type | Rules |
|---|---|---|
| `id` | UUID | Primary key |
| `username` | string | Unique, normalized, 3–64 chars |
| `password_hash` | string | Argon2id hash; never returned |
| `display_name` | string | 1–100 chars |
| `status` | enum | `ACTIVE`, `DISABLED`, `LOCKED` |
| `team_id` | UUID nullable | References `teams.id` |
| `acl_version` | integer | Incremented on any permission/scope change |
| `created_at`, `updated_at` | datetime | UTC |

**Relationships**: many-to-one Team; many-to-many Role through `user_roles`; owns Sessions,
Workflows, Tickets, Approval decisions, and Audit Events.

### Team (`teams`)

`id`, unique `code`, `name`, `status`, timestamps. Seed at least `employees`, `noc`, and `platform`.

### Role (`roles`) and User Role (`user_roles`)

Roles use fixed codes `EMPLOYEE`, `OPERATOR`, `APPROVER`, and `ADMIN`. `user_roles` has the unique
key `(user_id, role_id)`. Role assignment changes increment the user's `acl_version`.

### Login Session (`login_sessions`)

| Field | Type | Rules |
|---|---|---|
| `id` | UUID | JWT `sid`; primary key |
| `user_id` | UUID | Required owner |
| `token_family_id` | UUID | Groups refresh/reissue lineage if enabled |
| `status` | enum | `ACTIVE`, `REVOKED`, `EXPIRED` |
| `expires_at`, `revoked_at` | datetime nullable | Server enforced |
| `created_at`, `last_seen_at` | datetime | UTC |

## Workflow and Planning

### Workflow (`workflows`)

Durable projection used for ownership, status queries, scheduling, and checkpoint reconciliation.

| Field | Type | Rules |
|---|---|---|
| `id` | UUID | Public `workflow_id`; primary key |
| `thread_id` | UUID | Unique LangGraph checkpoint namespace |
| `owner_user_id` | UUID | Required; object authorization anchor |
| `session_id` | UUID nullable | Originating login session; not required for later access |
| `original_request` | text | Redacted/size-limited user goal |
| `status` | enum | State machine below |
| `plan_id` | UUID nullable | Current plan identity |
| `plan_version` | integer | Starts at 0; increments on clarification/edit |
| `deadline_at` | datetime nullable | Business workflow deadline |
| `pending_approval_id` | UUID nullable | Current approval if waiting |
| `last_operation_id` | UUID nullable | Last durably confirmed operation |
| `last_confirmed_stage` | string nullable | Recovery hint, not a substitute for the operation ledger |
| `active_run_id` | UUID nullable | Optimistic worker ownership |
| `version` | integer | Compare-and-swap workflow version |
| `accepted_at`, `completed_at`, `created_at`, `updated_at` | datetime nullable | UTC |

**State transitions**:

```text
RECEIVED -> CLARIFYING -> PLANNED -> RUNNING
RECEIVED/CLARIFYING -> REJECTED
RUNNING -> WAITING_APPROVAL -> RUNNING
RUNNING -> WAITING_RETRY -> RUNNING
RUNNING/WAITING_* -> CANCEL_REQUESTED -> CANCELLED
RUNNING/WAITING_* -> NEEDS_REVIEW
RUNNING -> SUCCEEDED | FAILED
WAITING_APPROVAL -> REJECTED | EXPIRED
```

Terminal states: `SUCCEEDED`, `FAILED`, `REJECTED`, `CANCELLED`, `EXPIRED`. `NEEDS_REVIEW` is
nonterminal but can advance only through an authorized recovery command.

### Plan (`workflow_plans`)

| Field | Type | Rules |
|---|---|---|
| `id` | UUID | Stable across clarification versions |
| `workflow_id` | UUID | Required |
| `version` | integer | Unique with workflow/plan |
| `schema_version` | integer | Structured plan contract version |
| `source_model`, `prompt_version` | string | Adapter/model evidence; `stub` allowed |
| `normalized_request_hash` | string | SHA-256 of canonical request/context subset |
| `status` | enum | `CANDIDATE`, `VALIDATED`, `SUPERSEDED`, `REJECTED` |
| `risk_level` | enum | `LOW`, `MEDIUM`, `HIGH`, `CRITICAL` |
| `validation_summary` | JSON | Deterministic validation results |
| `created_at` | datetime | UTC |

Unique key `(workflow_id, version)`. Only a `VALIDATED` plan can execute.

### Plan Task (`plan_tasks`)

| Field | Type | Rules |
|---|---|---|
| `id` | UUID | Stable within plan version |
| `plan_id`, `plan_version` | UUID, integer | Parent plan version |
| `intent` | enum | Six supported intentions |
| `source_span` | text | Size-limited excerpt that motivated the task |
| `slots` | JSON | Canonical validated parameters and provenance |
| `missing_slots` | JSON | Empty before plan becomes validated |
| `risk_level` | enum | Drives confirmation/approval |
| `operation_id` | UUID nullable | Stable UUIDv5 for side-effecting tasks |
| `position` | integer | Stable display order |

Unique `(plan_id, plan_version, id)` and unique non-null `operation_id`.

### Plan Dependency (`plan_dependencies`)

`plan_id`, `plan_version`, `from_task_id`, `to_task_id`, `kind`, and optional `output_binding`.
`kind` is `SEQUENCE`, `DATA`, `ON_SUCCESS`, or `ON_FAILURE`. The compiler rejects self-edges,
unknown tasks, cycles, and incompatible output bindings before insert.

### Workflow Command (`workflow_commands`)

Durable serialized input for messages, clarification answers, approval resumes, cancellations, and recovery.

| Field | Type | Rules |
|---|---|---|
| `id` | UUID/string | Client command or derived stable ID; unique |
| `workflow_id` | UUID | Required |
| `sequence` | integer | Unique per workflow; server assigned |
| `kind` | enum | `USER_MESSAGE`, `CLARIFICATION`, `APPROVAL_RESUME`, `CANCEL`, `RECOVER` |
| `expected_workflow_version` | integer nullable | Required for state-changing commands where known |
| `payload_hash` | string | Detects same-key/different-payload reuse |
| `payload` | JSON | Validated, size-limited |
| `status` | enum | `PENDING`, `PROCESSING`, `APPLIED`, `REJECTED`, `FAILED` |
| `result_summary` | JSON nullable | Stable replay response |
| timestamps | datetime | UTC |

Unique `(workflow_id, sequence)` and globally unique `id`.

## Ticketing and Approval

### Ticket (`tickets`)

| Field | Type | Rules |
|---|---|---|
| `id` | UUID | Primary key |
| `key` | string | Unique human ID, e.g. `INC-000001` |
| `title` | string | 1–200 chars |
| `description` | text | 1–10,000 chars |
| `severity` | enum | `P1`, `P2`, `P3`, `P4` |
| `status` | enum | `OPEN`, `IN_PROGRESS`, `RESOLVED`, `CLOSED`, `CANCELLED` |
| `created_by_user_id` | UUID | Required |
| `assigned_team_id`, `assignee_user_id` | UUID nullable | Authorization scope |
| `version` | integer | Starts at 1; every mutation increments |
| `sla_deadline` | datetime nullable | Current authoritative deadline |
| `sla_version` | integer | Increments when SLA basis changes |
| `escalation_level` | integer | Starts at 0 |
| `resolved_at`, `closed_at`, timestamps | datetime nullable | UTC |

**Transitions**:

```text
OPEN -> IN_PROGRESS | CANCELLED
IN_PROGRESS -> RESOLVED | CANCELLED
RESOLVED -> IN_PROGRESS | CLOSED
CLOSED and CANCELLED are terminal in MVP
```

P1 close/cancel and all sandbox operations require approval. Updates use `expected_version`.

### Ticket Event (`ticket_events`)

Append-only business history: `id`, `ticket_id`, `ticket_version`, `event_type`, `actor_user_id`,
`operation_id`, canonical before/after summaries, and `created_at`. Unique where one operation may
write only one event of the same type.

### Approval (`approvals`)

| Field | Type | Rules |
|---|---|---|
| `id` | UUID | Primary key |
| `workflow_id`, `plan_id`, `plan_version`, `task_id` | IDs | Exact plan binding |
| `operation_id` | UUID | Exact proposed side effect |
| `action_type` | string | Capability registry key |
| `resource_type`, `resource_id`, `resource_version` | strings | Exact target binding |
| `payload_hash` | string | Canonical action parameters |
| `requester_user_id` | UUID | Cannot be trusted from model input |
| `approver_user_id` | UUID nullable | Set on decision |
| `status` | enum | State machine below |
| `decision_reason` | string nullable | Required for rejection; optional for approval |
| `expires_at`, `decided_at`, timestamps | datetime nullable | UTC |
| `version` | integer | Conditional decision update |

**Transitions**: `PENDING -> APPROVED | REJECTED | EXPIRED | INVALIDATED`; every terminal decision is
immutable. Resume still rechecks payload, resource version, expiry, and authorization.

## Reliability and Messaging

### Operation Record (`operation_ledger`)

| Field | Type | Rules |
|---|---|---|
| `operation_id` | UUID | Primary key; stable across replay |
| `scope_type`, `scope_id` | string, UUID | User/workflow/tool uniqueness scope |
| `operation_type` | string | Capability or domain operation |
| `payload_hash` | string | Same ID with different hash is a conflict |
| `status` | enum | `CLAIMED`, `RUNNING`, `SUCCEEDED`, `FAILED_RETRYABLE`, `FAILED_TERMINAL`, `UNKNOWN` |
| `resource_type`, `resource_id`, `resource_version` | nullable | Result link |
| `result_summary` | JSON nullable | Bounded stable replay result |
| `attempt_count` | integer | Monotonic |
| `lease_owner`, `lease_until`, `heartbeat_at` | nullable | Safe takeover metadata |
| `last_error_code`, `last_error_summary` | nullable | Redacted |
| timestamps | datetime | UTC |

Unique `(scope_type, scope_id, operation_type, operation_id)`. Successful records are retained for
the audit/retry horizon; large result bodies are stored separately or truncated.

### Outbox Event (`outbox_events`)

`id`, unique `message_id`, `event_type`, `schema_version`, `aggregate_type`, `aggregate_id`,
`operation_id`, JSON payload, trace context, `status`, `attempt_count`, `next_attempt_at`,
`lease_owner`, `lease_until`, broker receipt, error summary, created/sent timestamps.

**Transitions**: `PENDING -> SENDING -> SENT`; `SENDING -> PENDING` after expired lease;
`PENDING/SENDING -> DEAD` after terminal error or attempt policy. Publishing happens outside the
leasing transaction, so `message_id` remains stable across every send attempt.

### Inbox Message (`inbox_messages`)

Composite primary key `(consumer_group, message_id)`. Other fields: event type/version, `status`
(`PROCESSING`, `DONE`, `DEAD`), payload hash, linked local resource/result, attempts, last error, and
timestamps. Inbox insert/complete and local business changes share one MySQL transaction.

### Notification Delivery (`notification_deliveries`)

`id`, unique `operation_id`, source `message_id`, channel (`MAIL_CAPTURE`, `WEBHOOK_SANDBOX`),
recipient scope, content template/version and data, `status`, provider key, attempts,
`next_attempt_at`, `last_error`, `unknown_since`, delivered timestamp, and audit timestamps.

**Transitions**: `PENDING -> SENDING -> DELIVERED`; transient error returns to `PENDING`; ambiguous
timeout becomes `UNKNOWN -> DELIVERED | FAILED` through reconciliation; permanent failure becomes `FAILED`.

## Knowledge

### Document (`documents`)

`id`, title, source URI/name, owner team, visibility mode (`PUBLIC`, `ROLE`, `TEAM`, `EXPLICIT`),
active version ID, status (`ACTIVE`, `ARCHIVED`), created-by user, and timestamps.

### Document ACL Grant (`document_acl_grants`)

`document_id`, `principal_type` (`ROLE`, `TEAM`, `USER`), `principal_id`, and grant timestamps.
Unique `(document_id, principal_type, principal_id)`. `PUBLIC` documents need no grant rows.

### Document Version (`document_versions`)

| Field | Type | Rules |
|---|---|---|
| `id`, `document_id` | UUID | Primary/parent |
| `version` | integer | Unique with document |
| `checksum` | string | Source content SHA-256 |
| `media_type`, `source_location` | string | Bounded MVP formats/location |
| `normalized_text` | large text nullable | Rebuild source after successful parse |
| `parser_version`, `chunker_version`, `embedding_version` | string | Reproducibility |
| `status` | enum | `REGISTERED`, `QUEUED`, `PARSING`, `INDEXING`, `READY`, `FAILED`, `SUPERSEDED` |
| `failure_stage`, `failure_summary` | nullable | Diagnosable, redacted |
| counts and timestamps | integer/datetime | Page/segment/token estimates and lifecycle |

Only a fully `READY` version may atomically become `documents.active_version_id`.

### Document Segment (`document_segments`)

`id`, `document_version_id`, sequence, section path, page/anchor, normalized text, content hash,
token count, and active flag. This is the authoritative citation content. Milvus uses the same segment
ID with dense vector, BM25 sparse field, document/version IDs, and compact ACL scope tokens.

### Retrieval Evidence (`retrieval_evidence`)

Bounded audit/evaluation record: `id`, workflow/task, query hash or redacted text, user scope hash and
ACL version, retrieval configuration version, segment ID, rank, dense/BM25/fused scores, selected flag,
and timestamp. Full sensitive text is not copied into general logs.

## Audit and Evaluation

### Audit Event (`audit_events`)

Append-only `id`, `occurred_at`, actor user/session, request/trace/workflow/plan/task/operation/message
links, action, resource, authorization decision, outcome, reason code, and redacted metadata. Audit
rows cannot be updated through normal application repositories.

### Evaluation Run (`evaluation_runs`)

`id`, suite (`INTENT`, `RAG`, `WORKFLOW`, `FAULT`, `LOAD`, `REAL_MODEL`), dataset/version, code revision,
configuration hash, model/adapter/prompt/retrieval versions, environment JSON, started/finished times,
status, sample counts, metrics JSON, error counts, and report path.

### Evaluation Result (`evaluation_results`)

`run_id`, `case_id`, expected summary, actual summary, metrics, pass/fail, error category, trace/workflow
links, and artifact path. Unique `(run_id, case_id)`.

## Cross-Entity Invariants

1. A model-produced ID or user identity is never used until resolved and authorized server-side.
2. A validated plan has no missing slots, unsupported intents/tools, invalid edges, or cycles.
3. A side-effecting Plan Task has exactly one stable Operation Record identity across retries/resumes.
4. A successful ticket/approval operation and its logical Outbox event commit together.
5. A consumer's Inbox completion and local MySQL business effect commit together.
6. An Approval can authorize only its bound operation, plan version, payload hash, resource version,
   and unexpired policy context.
7. A Workflow projection can lag a Redis checkpoint, but recovery always checks MySQL business and
   operation records before performing a side effect.
8. A Milvus segment is served only if the matching active MySQL Document Version and current ACL
   both authorize it.
9. An SLA trigger applies only when its expected SLA version and current unresolved ticket state match.
10. Documentation may call a number measured only when a completed Evaluation Run and report support it.
