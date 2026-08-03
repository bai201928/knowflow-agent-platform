---
description: "Dependency-ordered implementation tasks for the KnowFlow two-week MVP"
---

# Tasks: KnowFlow Reliable Agent Platform

**Input**: Design documents from `specs/001-knowflow-agent-platform/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md),
[data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md)

**Tests**: Required by FR-028 and Constitution Principle V. Within every story, tests are written
first and must fail for the intended reason before implementation begins.

**Organization**: Tasks are grouped by user story. Each story ends with an independently runnable
acceptance checkpoint; tasks use the exact checklist format and repository-relative paths.

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Establish the installable Python project, local dependencies, and safe configuration.

- [X] T001 Create the Python 3.12 project metadata, runtime/dev dependency groups, CLI entry point, Ruff, mypy, and pytest configuration in `pyproject.toml`
- [X] T002 [P] Create the modular package/test/data/report directory skeleton and package markers under `src/knowflow/`, `tests/`, `data/`, and `reports/`
- [X] T003 [P] Define healthy local MySQL 8.4, Redis 8, Milvus/etcd/MinIO, RocketMQ 5.5 proxy, and Mailpit services in `docker-compose.yml`
- [X] T004 [P] Document non-secret settings, stub/real model modes, dependency URLs, limits, and sandbox defaults in `.env.example`
- [X] T005 [P] Configure Alembic metadata loading and async migration connectivity in `alembic.ini` and `alembic/env.py`
- [X] T006 [P] Exclude secrets, local state, caches, generated traces, and transient evaluation outputs while retaining evidence reports in `.gitignore`

**Checkpoint**: `uv sync --all-groups` resolves, Compose validates, and imports find `knowflow`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Build security, storage, state, contracts, and test primitives required by all stories.

**⚠️ CRITICAL**: No user-story implementation starts until this phase passes its checkpoint.

- [X] T007 [P] Implement typed environment loading, secret validation, per-runtime settings, and safe defaults in `src/knowflow/config.py`
- [X] T008 [P] Implement request/workflow/plan/task/operation/message ID creation, stable UUIDv5 operations, canonical JSON, and payload hashing in `src/knowflow/domain/common/identity.py`
- [X] T009 [P] Define domain/application error codes, retryability, concealed-not-found behavior, and RFC 9457 problem mapping in `src/knowflow/domain/common/errors.py`
- [X] T010 [P] Implement monotonic absolute deadlines, remaining-budget propagation, bounded retry policy, and cancellation classifications in `src/knowflow/domain/common/deadlines.py`
- [X] T011 Configure async SQLAlchemy engine/session factories, transaction unit-of-work, UTC helpers, naming conventions, and health probes in `src/knowflow/infrastructure/db/session.py`
- [X] T012 [P] Map User, Team, Role, UserRole, LoginSession, and immutable audit records with required constraints in `src/knowflow/infrastructure/db/models/identity.py`
- [X] T013 [P] Map Workflow, Plan, PlanTask, PlanDependency, WorkflowCommand, OperationRecord, OutboxEvent, and InboxMessage in `src/knowflow/infrastructure/db/models/workflow.py`
- [X] T014 [P] Map Ticket, TicketEvent, Approval, and NotificationDelivery state/version fields and constraints in `src/knowflow/infrastructure/db/models/ticketing.py`
- [X] T015 [P] Map Document, DocumentACLGrant, DocumentVersion, DocumentSegment, RetrievalEvidence, EvaluationRun, and EvaluationResult in `src/knowflow/infrastructure/db/models/knowledge.py`
- [X] T016 Create the initial MySQL schema, indexes, unique keys, foreign keys, and state checks from the four model groups in `alembic/versions/0001_initial_schema.py`
- [ ] T017 Implement Argon2id password verification, short-lived JWT issue/validation, session revocation, and trusted claims in `src/knowflow/application/auth/service.py`
- [ ] T018 Implement immutable `AccessContext`, four-role capability rules, team/owner object checks, and ACL scope-token generation in `src/knowflow/application/auth/policy.py`
- [ ] T019 Wire authentication, request/trace IDs, deadline creation, access-context loading, and problem responses in `src/knowflow/api/dependencies.py` and `src/knowflow/api/error_handlers.py`
- [ ] T020 Create the FastAPI lifespan, router registry, dependency health endpoint, and native OpenAPI metadata in `src/knowflow/api/main.py`
- [ ] T021 Define async chat/embedding ports, the OpenAI-compatible adapter, deterministic structured stub, and provider error normalization in `src/knowflow/infrastructure/models/adapters.py`
- [ ] T022 Configure async Redis clients, Redis checkpoint index setup, session/cache namespacing, token buckets, and lease helpers in `src/knowflow/infrastructure/redis/client.py`
- [ ] T023 Define versioned event envelopes, the event catalog, publisher/consumer ports, and payload hash validation matching `contracts/events.md` in `src/knowflow/domain/messaging/events.py`
- [ ] T024 Configure structured redacted logs, OpenTelemetry trace/metric providers, context links, and no-op local exporters in `src/knowflow/infrastructure/observability/bootstrap.py`
- [ ] T025 Build pytest configuration, async database cleanup, seeded users, deterministic adapters, clock control, and dependency overrides in `tests/conftest.py`
- [X] T026 [P] Add schema tests for local `$ref` resolution, operation IDs, event examples, state enums, and problem responses in `tests/contract/test_foundation_contracts.py`

**Checkpoint**: Migrations apply/rollback, seeded authentication works, foundational contract tests pass,
and no test/user input can override the trusted access context.

---

## Phase 3: User Story 1 — Resolve an Incident Through One Reliable Request (Priority: P1) 🎯 MVP

**Goal**: Complete the flagship request from cited diagnosis through at-least-once delivery with non-duplicated business effects,
notification, approval pause/resume, and one sandbox operation.

**Independent Test**: Submit the canonical RocketMQ backlog request as an employee, approve as an
approver, and verify one cited answer, ticket, logical notification, approval, sandbox operation,
and auditable terminal workflow even after repeated submits/resumes.

### Tests for User Story 1

- [ ] T027 [P] [US1] Write failing REST/SSE contract tests for workflow creation/status/messages/events, workflow audit timeline, recovery review/decision, and approval decision endpoints in `tests/contract/test_workflow_approval_contract.py`
- [ ] T028 [P] [US1] Write a failing end-to-end canonical incident/approval scenario with exact final invariants in `tests/e2e/test_flagship_incident_workflow.py`
- [ ] T029 [P] [US1] Write failing ambiguity/missing-slot tests proving no write effect occurs before clarification in `tests/integration/test_clarification_guard.py`
- [ ] T030 [P] [US1] Write failing repeated request, duplicate approval, and concurrent resume tests for one business effect in `tests/integration/test_flagship_idempotency.py`

### Implementation for User Story 1

- [ ] T031 [P] [US1] Define six intent schemas, slot provenance/trust levels, task/dependency/risk models, and clarification outcomes in `src/knowflow/domain/workflows/schemas.py`
- [ ] T032 [P] [US1] Define the versioned intent catalog, capability registry, required slots, permissions, side-effect flags, deadlines, and approval policies in `src/knowflow/domain/workflows/catalog.py`
- [ ] T033 [US1] Implement candidate intent recall, structured model planning, prompt/version recording, and stub plan fixtures in `src/knowflow/application/workflows/planner.py`
- [ ] T034 [US1] Implement deterministic slot resolution, resource authorization, DAG cycle/type validation, operation-ID allocation, and plan compilation in `src/knowflow/application/workflows/compiler.py`
- [ ] T035 [P] [US1] Define compact LangGraph State, schema/plan versions, owned single-value fields, ID-deduping reducers, and persisted evidence references in `src/knowflow/workflows/state.py`
- [ ] T036 [US1] Implement operation-ledger claim/replay/conflict/success behavior and atomic ticket-create result reuse in `src/knowflow/application/workflows/operations.py`
- [ ] T037 [US1] Implement idempotent P1 ticket creation with TicketEvent, AuditEvent, Workflow projection, and Outbox in one unit of work in `src/knowflow/application/tickets/create.py`
- [ ] T038 [US1] Implement the minimal real hybrid retrieval port with server-built ACL filters, RRF evidence selection, and evidence-insufficient disposition in `src/knowflow/application/knowledge/retrieval.py`
- [ ] T039 [US1] Implement Milvus collection bootstrap, dense/BM25 hybrid query, stable segment IDs, and ACL scope-token filters in `src/knowflow/infrastructure/retrieval/milvus.py`
- [ ] T040 [US1] Implement approval creation, exact plan/payload/resource binding, one-time decision, expiry/invalidation, and resume-command transaction in `src/knowflow/application/approvals/service.py`
- [ ] T041 [P] [US1] Implement durable notification registration and recipient-scope resolution without direct network delivery in `src/knowflow/application/notifications/service.py`
- [ ] T042 [P] [US1] Implement the allowlisted consumer-restart sandbox with stable operation IDs, status lookup, deadline, and repeat-safe outcomes in `src/knowflow/infrastructure/operations/sandbox.py`
- [ ] T043 [P] [US1] Implement append-only linked audit writes and redacted workflow timeline reads in `src/knowflow/application/audit/service.py`
- [ ] T044 [US1] Implement planner, clarification, retrieval, ticket, notification, approval interrupt, sandbox operation, and final-summary nodes in `src/knowflow/workflows/nodes/incident.py`
- [ ] T045 [US1] Assemble conditional routes, task dependencies, interrupt/resume, retry boundaries, and Redis checkpointer compilation in `src/knowflow/workflows/graph.py`
- [ ] T046 [US1] Implement durable workflow acceptance, message/clarification commands, projection reads, version ownership, and graph dispatch in `src/knowflow/application/workflows/service.py`
- [ ] T047 [P] [US1] Persist monotonically sequenced workflow events and authorized replay windows for SSE reconnect in `src/knowflow/application/workflows/events.py`
- [ ] T048 [US1] Implement create/list/get/message workflow endpoints and idempotency conflicts from `contracts/openapi.yaml` in `src/knowflow/api/routes/workflows.py`
- [ ] T049 [P] [US1] Implement approval list/get/decision endpoints with role/object checks and concealed not-found behavior in `src/knowflow/api/routes/approvals.py`
- [ ] T050 [P] [US1] Implement native SSE workflow event streaming with authorization, durable sequence IDs, reconnect, keepalive, and disconnect cleanup in `src/knowflow/api/routes/workflow_events.py`
- [ ] T051 [P] [US1] Wrap Apache RocketMQ 5.x producer/consumer lifecycle, gRPC configuration, deadlines, and telemetry in `src/knowflow/infrastructure/messaging/rocketmq.py`
- [ ] T052 [US1] Implement short-transaction Outbox leasing, network publish outside locks, stable message IDs, retry/dead state, and lease recovery in `src/knowflow/workers/outbox.py`
- [ ] T053 [US1] Implement serialized workflow command consumption, Redis checkpoint resume, and safe active-run/version release in `src/knowflow/workers/workflow.py`
- [ ] T054 [US1] Implement Inbox-deduped Mailpit notification delivery and repeat-safe delivery states in `src/knowflow/workers/notifications.py`
- [ ] T055 [US1] Seed demo roles/users, canonical manual segments/indexes, sandbox resource, and the flagship request fixtures in `src/knowflow/cli.py`
- [ ] T056 [US1] Build the minimal login, workflow prompt, streamed timeline, plan, citation, ticket, and approval panels in `src/knowflow/web/templates/demo.html` and `src/knowflow/web/static/demo.js`

**Checkpoint**: User Story 1 passes T027–T030 and can be demonstrated alone from the seeded data.

---

## Phase 4: User Story 2 — Get Permission-Safe, Cited Knowledge Answers (Priority: P2)

**Goal**: Provide versioned document ingestion plus direct grounded answers with three-layer ACL and
re-authorized citations.

**Independent Test**: Ingest public and protected document pairs, then verify answerable,
unanswerable, stale-version, cache-scope, indirect-injection, and unauthorized citation cases.

### Tests for User Story 2

- [ ] T057 [P] [US2] Write failing document/immutable-version/detail/version-retry plus knowledge/citation OpenAPI contract tests in `tests/contract/test_knowledge_document_contract.py`
- [ ] T058 [P] [US2] Write failing paired-document ACL, cache-scope, citation reauthorization, and trace-redaction tests in `tests/integration/test_knowledge_acl.py`
- [ ] T059 [P] [US2] Write failing ingestion-version, parse failure/retry, hybrid retrieval, citation, refusal, and prompt-injection E2E tests in `tests/e2e/test_knowledge_story.py`

### Implementation for User Story 2

- [ ] T060 [P] [US2] Implement bounded plain-text/Markdown/PDF parsing, heading-aware chunking, hashes, and deterministic segment IDs in `src/knowflow/application/knowledge/ingestion.py`
- [ ] T061 [US2] Implement document registration, immutable version creation/detail, ACL grants, failed-version retry attempts with stable checksums/idempotency, and atomic ready-version switching in `src/knowflow/application/knowledge/documents.py`
- [ ] T062 [US2] Implement Inbox-deduped parse/index consumption, stage diagnostics, batch embeddings, completeness checks, and retry/dead behavior in `src/knowflow/workers/documents.py`
- [ ] T063 [US2] Add document-version rebuild, compact ACL metadata refresh, post-retrieval reauthorization, and safe cache keys to `src/knowflow/infrastructure/retrieval/milvus.py`
- [ ] T064 [US2] Implement context budgeting, evidence numbering, untrusted-document boundaries, grounded answer schema, and server-side citation validation in `src/knowflow/application/knowledge/answering.py`
- [ ] T065 [US2] Implement direct query and citation endpoints with current ACL/version checks in `src/knowflow/api/routes/knowledge.py`
- [ ] T066 [US2] Implement admin document register/list/get plus explicit create-version/get-version/retry-version endpoints and durable ingestion events in `src/knowflow/api/routes/documents.py`
- [ ] T067 [US2] Add bounded public manuals, protected document pairs, answerable/no-answer questions, and expected segment relevance in `data/eval/rag-v1.jsonl`

**Checkpoint**: User Story 2 passes T057–T059 independently through the direct knowledge APIs.

---

## Phase 5: User Story 3 — Manage Tickets Within Role and Object Permissions (Priority: P2)

**Goal**: Complete direct ticket creation/query/update, optimistic concurrency, durable events,
notifications, and versioned SLA checks.

**Independent Test**: Two employees and one operator exercise permitted/forbidden objects, repeated
create, same-version updates, and current/obsolete SLA triggers without the flagship agent route.

### Tests for User Story 3

- [ ] T068 [P] [US3] Write failing create/list/get/update ticket, ticket audit timeline, notification summary, and notification-delivery query contract tests including idempotency and optimistic versions in `tests/contract/test_ticket_contract.py`
- [ ] T069 [P] [US3] Write failing cross-user/team object authorization, same-key/different-payload, and concurrent update tests in `tests/integration/test_ticket_security_concurrency.py`
- [ ] T070 [P] [US3] Write failing direct ticket lifecycle, notification summary/detail state, current SLA escalation, and obsolete-trigger E2E tests in `tests/e2e/test_ticket_story.py`

### Implementation for User Story 3

- [ ] T071 [P] [US3] Implement access-scoped ticket reads, cursor pagination, versioned conditional updates, state transitions, and conflict snapshots in `src/knowflow/infrastructure/db/repositories/tickets.py`
- [ ] T072 [US3] Implement direct create/query/update use cases with idempotency, sensitive P1 approval conversion, TicketEvent, AuditEvent, and Outbox boundaries in `src/knowflow/application/tickets/service.py`
- [ ] T073 [US3] Implement create/list/get/patch endpoints with notification summaries, concealed object authorization, and replay metadata in `src/knowflow/api/routes/tickets.py`
- [ ] T074 [P] [US3] Define severity deadlines, pause/resume rules, SLA versions, escalation levels, and notification policy in `src/knowflow/domain/tickets/sla.py`
- [ ] T075 [US3] Schedule stable RocketMQ timed checks whenever the authoritative SLA version changes in `src/knowflow/application/tickets/sla.py`
- [ ] T076 [US3] Implement Inbox-deduped SLA checks that reread current ticket state/version, apply one escalation, and emit notification Outbox records in `src/knowflow/workers/sla.py`
- [ ] T077 [P] [US3] Add ticket list/detail/version-conflict and notification-state panels to `src/knowflow/web/templates/demo.html` and `src/knowflow/web/static/demo.js`
- [ ] T078 [P] [US3] Seed two-user ownership/team fixtures, ticket state examples, and current/obsolete SLA cases in `data/seed/tickets.json`
- [ ] T079 [US3] Add ticket/SLA/notification trace attributes and correctness counters without sensitive descriptions in `src/knowflow/infrastructure/observability/ticketing.py`

**Checkpoint**: User Story 3 passes T068–T070 independently through direct ticket APIs.

---

## Phase 6: User Story 4 — Recover and Audit Interrupted Workflows (Priority: P3)

**Goal**: Prove convergence after replay, duplicate delivery, stale checkpoints, conflicting workers,
disconnects, uncertain external outcomes, and all five governed failures.

**Independent Test**: Run `scripts/fault-injection.ps1 -Scenario all` and verify every final invariant,
recovery state, trace, and report without manual database repair.

### Tests for User Story 4

- [ ] T080 [P] [US4] Write a failing post-MySQL-commit/pre-checkpoint process-exit and concurrent-resume test in `tests/fault/test_commit_before_checkpoint.py`
- [ ] T081 [P] [US4] Write a failing consumer-local-commit/pre-ack exit and repeated-message test in `tests/fault/test_commit_before_mq_ack.py`
- [ ] T082 [P] [US4] Write failing duplicate approve/reject, duplicate resume, and expired/changed-resource approval tests in `tests/fault/test_duplicate_approval_resume.py`
- [ ] T083 [P] [US4] Write failing stale/missing Redis checkpoint reconciliation and high-risk UNKNOWN review tests in `tests/fault/test_stale_missing_checkpoint.py`
- [ ] T084 [P] [US4] Write failing same-version concurrent ticket/workflow update and expired Redis-lease stale-writer tests in `tests/fault/test_concurrent_stale_update.py`

### Implementation for User Story 4

- [ ] T085 [P] [US4] Implement named deterministic failpoints at database commit, checkpoint advance, broker ack, approval resume, and version update boundaries in `src/knowflow/infrastructure/testing/failpoints.py`
- [ ] T086 [US4] Add operation lease takeover, stale result reconciliation, UNKNOWN handling, payload conflicts, and repeat-safe result reconstruction to `src/knowflow/application/workflows/operations.py`
- [ ] T087 [US4] Add publisher crash recovery, lease-owner conditional updates, poison-event isolation, and broker-receipt attempts to `src/knowflow/workers/outbox.py`
- [ ] T088 [P] [US4] Implement a reusable Inbox executor that atomically deduplicates and commits local effects before ack in `src/knowflow/infrastructure/messaging/inbox.py`
- [ ] T089 [US4] Implement per-workflow command sequencing, active-run compare-and-swap, Redis thread leases, renewal, and stale-run rejection in `src/knowflow/application/workflows/ownership.py`
- [ ] T090 [US4] Implement MySQL projection versus Redis checkpoint reconciliation, result replay, low-risk reconstruction, and `NEEDS_REVIEW` routing in `src/knowflow/workflows/recovery.py`
- [ ] T091 [P] [US4] Implement pre/post durable-acceptance disconnect behavior and versioned explicit cancellation/compensation policy in `src/knowflow/application/workflows/cancellation.py`
- [ ] T092 [US4] Implement scans for missing/stale checkpoints, expired operation/Outbox leases, overdue SLA checks, and UNKNOWN notifications in `src/knowflow/workers/reconciliation.py`
- [ ] T093 [US4] Implement provider-status reconciliation and terminal handling for ambiguous sandbox notification outcomes in `src/knowflow/application/notifications/reconciliation.py`
- [ ] T094 [P] [US4] Implement cursor-paginated, resource-scoped workflow/ticket audit timeline endpoints with operation replay visibility, duplicate counts, recovery reasons, authorization, and linked-object redaction in `src/knowflow/api/routes/audit.py`
- [ ] T095 [US4] Complete workflow cancellation, recovery-review, idempotent recovery-decision actions (`RESUME_FROM_FACTS`, `RETRY_SAFE_STEP`, `MARK_FAILED`, `REQUIRE_NEW_APPROVAL`), conflict, and detailed status responses in `src/knowflow/api/routes/workflows.py`
- [ ] T096 [US4] Propagate cancellation through pure retrieval/model TaskGroups while shielding durably accepted commands in `src/knowflow/workflows/graph.py`
- [ ] T097 [US4] Orchestrate targeted worker/container exits, checkpoint rollback, concurrent resumes, invariant queries, and artifact capture in `scripts/fault-injection.ps1`
- [ ] T098 [US4] Persist scenario environment, failpoint, trace IDs, database invariants, duplicate observations, and pass/fail summaries in `src/knowflow/evaluation/fault_report.py`

**Checkpoint**: All five tests T080–T084 pass and produce evidence under `reports/fault/`.

---

## Phase 7: User Story 5 — Produce Honest Quality and Performance Evidence (Priority: P3)

**Goal**: Generate versioned component, workflow, fault, real-model, and controlled-load evidence
whose numbers can safely be used in the README, interview, and resume.

**Independent Test**: Run the three deterministic suites, one 20-user stub load, and a bounded real
model suite; verify provenance, separated conclusions, and pending labels for absent measurements.

### Tests for User Story 5

- [ ] T099 [P] [US5] Write failing tests for F1, exact match, Recall@K, MRR, nDCG, citation, refusal, duplicate-effect, and recovery metrics in `tests/evaluation/test_metrics.py`
- [ ] T100 [P] [US5] Write failing contract tests for evaluation/load report provenance, pending values, and stub-versus-real labels in `tests/contract/test_report_contract.py`

### Implementation for User Story 5

- [ ] T101 [P] [US5] Create versioned single/multi-intent, slot, OOS, ambiguity, authorization, and injection cases in `data/eval/intent-v1.jsonl`
- [ ] T102 [P] [US5] Create expected workflow DAG, approval, tool, fault, and final-invariant cases in `data/eval/workflow-v1.jsonl`
- [ ] T103 [P] [US5] Create a bounded real-model smoke set with redacted inputs and explicit expected dispositions in `data/eval/real-model-smoke-v1.jsonl`
- [ ] T104 [US5] Implement intent/slot/OOS/calibration calculations and per-risk error breakdowns in `src/knowflow/evaluation/intent.py`
- [ ] T105 [US5] Implement retrieval, answer, citation, refusal, and ACL-zero-leak calculations in `src/knowflow/evaluation/rag.py`
- [ ] T106 [US5] Implement DAG/tool/approval/task success, duplicate-effect, and recovery convergence metrics in `src/knowflow/evaluation/workflow.py`
- [ ] T107 [US5] Implement immutable run provenance, per-case results, aggregate JSON/Markdown, target-versus-measured labels, and report links in `src/knowflow/evaluation/reporting.py`
- [ ] T108 [US5] Implement suite selection, dataset locking, stub/real adapter selection, bounded case count, and nonzero failure exits in `src/knowflow/evaluation/runner.py`
- [ ] T109 [US5] Integrate Langfuse Python v4 LLM observations/scores with OTel context, sampling, and input/output redaction in `src/knowflow/infrastructure/observability/langfuse.py`
- [ ] T110 [US5] Instrument API, workflow nodes, model/retrieval calls, DB/MQ waits, retries, queues, and correctness counters in `src/knowflow/infrastructure/observability/instrumentation.py`
- [ ] T111 [US5] Implement realistic knowledge/ticket/approval/status/document request mixes, user fairness, correctness assertions, and CSV metadata in `tests/load/locustfile.py`
- [ ] T112 [US5] Expose `evaluate`, `load-metadata`, and report-index commands with measured-only summary output in `src/knowflow/cli.py`

**Checkpoint**: User Story 5 passes T099–T100; reports under `reports/evaluation/` and `reports/load/`
contain enough provenance to reproduce results and never mix stub capacity with real-model quality.

---

## Phase 8: Polish & Cross-Cutting Gates

**Purpose**: Close security, reproducibility, documentation, and full-system delivery gaps.

- [ ] T113 [P] Add direct and indirect prompt injection, arbitrary URL/tool, cross-thread, cache-scope, role spoofing, and sensitive-trace regression cases in `tests/e2e/test_security_red_team.py`
- [ ] T114 [P] Add database/Redis/Milvus/RocketMQ/model dependency degradation and recovery matrix tests in `tests/integration/test_dependency_degradation.py`
- [ ] T115 Enforce Ruff formatting/lint, mypy strict boundaries, import layering, and secret scanning in `pyproject.toml` and `.pre-commit-config.yaml`
- [ ] T116 Implement safe start/stop/readiness behavior for all runtime roles in `scripts/dev.ps1`
- [ ] T117 Implement the canonical demo, approval handoff, duplicate replay, evidence-link sequence, and five-participant/20-attempt usability protocol template in `scripts/demo.ps1` and `reports/usability/protocol.md`
- [ ] T118 Validate every command and expected outcome in `specs/001-knowflow-agent-platform/quickstart.md`, correcting documentation without weakening acceptance criteria
- [ ] T119 Write architecture, state-authority, transaction, threat-model, failure semantics, metrics caveats, and demo instructions in `README.md`
- [ ] T120 Replace only actually measured placeholders and link raw report artifacts in `KnowFlow_41道核心面试题_5分钟回答.md`
- [ ] T121 Capture the final contract snapshot, schema revision, test summary, evaluation/load/fault/usability report index (including 18-of-20 threshold and interventions), and resume-ready evidence checklist in `reports/README.md`

**Final Checkpoint**: The quickstart runs from a clean local environment, every selected story and
governed test passes, and no documentation contains a fabricated result or production claim.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 — Setup**: no dependencies; T002–T006 can proceed after T001's package decisions are fixed.
- **Phase 2 — Foundational**: depends on Phase 1 and blocks all stories. T012–T015 are parallel model
  groups; T016 combines them into the initial migration.
- **Phase 3 — US1**: depends on Phase 2 and is the mandatory demonstration MVP.
- **Phase 4 — US2**: depends on the foundational schema and the retrieval/worker ports established by
  US1; its direct knowledge acceptance suite does not require the flagship workflow endpoint.
- **Phase 5 — US3**: depends on the foundational schema and shared operation/Outbox primitives from
  US1; its direct ticket acceptance suite does not require model planning or knowledge retrieval.
- **Phase 6 — US4**: depends on US1 and the ticket concurrency behavior from US3; it hardens recovery
  across all prior business paths.
- **Phase 7 — US5**: can begin dataset/metric work after Phase 2, but final reports require the stories
  they measure and the US4 fault evidence.
- **Phase 8 — Polish**: depends on all stories selected for the final demo.

### User Story Dependency Graph

```text
Setup -> Foundation -> US1 (flagship MVP)
                         |-> US2 (knowledge depth) --|
                         |-> US3 (ticket depth) ----|-> US4 (recovery proof) -> US5 (evidence)
Foundation -------------------------------> US5 dataset/metric scaffolding
All selected stories -------------------------------------------------> Polish
```

### Independent Test Criteria

- **US1**: canonical compound request completes with one citation/ticket/notification/approval/action.
- **US2**: direct knowledge APIs ingest and answer/refuse correctly with zero cross-scope leakage.
- **US3**: direct ticket APIs enforce scope, idempotency, optimistic conflict, and current SLA semantics.
- **US4**: five deterministic failures converge without duplicate facts and produce audit evidence.
- **US5**: versioned suites and load runs produce reproducible, honestly labeled reports.

### Within Each Story

1. Write the story's contract/integration/E2E or fault tests and confirm intended failures.
2. Implement domain models/policies before application services.
3. Implement infrastructure adapters before workers that call them.
4. Implement routes/UI only after use-case contracts stabilize.
5. Run the independent story test and preserve evidence before moving to the next checkpoint.

## Parallel Opportunities

### Shared Foundation

After T001 defines dependencies, T002–T006 can be split by file. After T011 defines DB conventions,
T012–T015 can be implemented in parallel, followed by T016. T021–T024 use separate adapters.

### User Story 1

```text
Parallel tests: T027, T028, T029, T030
Parallel definitions after tests: T031, T032, T035
Parallel adapters after core contracts: T041, T042, T043, T047, T049, T050, T051
```

### User Story 2

```text
Parallel tests: T057, T058, T059
Parallel early implementation: T060 and T067
Parallel routes after services: T065 and T066
```

### User Story 3

```text
Parallel tests: T068, T069, T070
Parallel domain/data work: T071, T074, T078
Parallel UI/observability after API behavior: T077 and T079
```

### User Story 4

```text
Parallel fault tests: T080, T081, T082, T083, T084
Parallel helpers: T085, T088, T091, T094
```

### User Story 5

```text
Parallel tests: T099 and T100
Parallel datasets: T101, T102, T103
Parallel metric engines: T104, T105, T106
Parallel observability/load work after reporting contract: T109, T110, T111
```

## Implementation Strategy

### MVP First

1. Complete Phase 1 and Phase 2.
2. Complete all US1 tests and implementation through T056.
3. Stop and run only the flagship acceptance checkpoint.
4. If the result is demo-ready, preserve a tag/report before adding breadth.

### Two-Week Incremental Delivery

1. **Days 1–2**: T001–T026 (setup, schema, auth, core ports).
2. **Days 3–9**: T027–T056 (flagship vertical slice), while starting T057–T067 retrieval depth.
3. **Days 8–11**: T068–T079 (direct ticket/SLA) and remaining knowledge work.
4. **Day 12**: T080–T098 (failure/recovery evidence).
5. **Days 13–14**: T099–T121 (evaluation, load, security, docs, final evidence).

If the schedule slips, complete US1, the US4 tests directly protecting its effects, and one honest
US5 report. Defer document-format breadth, SLA UI polish, optional Langfuse export, and secondary
evaluation cases before weakening authentication, ACL, approval, idempotency, or audit invariants.

## Notes

- `[P]` means the task changes different files and has no dependency on an unfinished task in the
  same listed parallel group.
- Every story task carries `[US1]`–`[US5]`; setup, foundation, and polish tasks intentionally do not.
- Exact source paths follow [plan.md](plan.md); contract behavior follows [contracts/](contracts/).
- Tests precede implementation because the constitution explicitly requires evidence-backed claims.
- Commit after each coherent task or tightly coupled transaction-boundary group; never commit secrets.
