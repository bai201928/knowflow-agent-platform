# Implementation Plan: KnowFlow Reliable Agent Platform

**Branch**: `agent/implement-knowflow-mvp` | **Date**: 2026-08-03 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/001-knowflow-agent-platform/spec.md`

**Note**: This plan ends at design. Source code is created only by the implementation workflow.

## Summary

Build a Python 3.12 modular monolith with separately runnable API, workflow, Outbox, and consumer
processes. An authenticated natural-language request is compiled into a validated task DAG and
executed by LangGraph. Redis persists graph checkpoints, while MySQL remains authoritative for
identity, permissions, tickets, approvals, workflow projections, operations, audit, Outbox, and
Inbox. Milvus provides ACL-filtered dense plus BM25 retrieval with RRF reranking and citations;
RocketMQ 5.x carries document, notification, SLA, and recovery events with at-least-once semantics.
The two-week deliverable prioritizes one complete incident-response workflow, a small server-rendered
demo UI, real failure/replay tests, and reproducible quality/load reports.

## Technical Context

**Language/Version**: Python 3.12

**Primary Dependencies**: FastAPI with native SSE, Uvicorn, Pydantic v2/pydantic-settings,
LangGraph, `langgraph-checkpoint-redis`, SQLAlchemy 2, Alembic, `asyncmy`, `redis`, `pymilvus`,
`rocketmq-python-client` from Apache RocketMQ's 5.x gRPC client repository, `httpx`, PyJWT,
`pwdlib[argon2]`, `pypdf` for bounded PDF text extraction, OpenTelemetry Python, Langfuse Python v4,
Jinja2, structlog, tenacity, and Typer. PDF parsing rejects encrypted, oversized, or textless files
with stable diagnostics; OCR and active-content execution are out of scope.

**Storage**: MySQL 8.4 LTS for business truth; Redis 8 for checkpoint/session/limit/cache state;
Milvus 2.5+ with MinIO and etcd for derived dense/BM25 indexes; local files only for bounded seed
documents and generated evaluation reports

**Testing**: pytest, pytest-asyncio, pytest-cov, HTTPX ASGI client, Docker-backed integration tests,
schema/contract tests, deterministic model/tool adapters, Hypothesis for idempotency/state-machine
properties, five required fault-injection suites, and Locust for controlled load

**Target Platform**: Linux containers on a single developer workstation; development commands must
work from Windows PowerShell through Docker Desktop and `uv`

**Project Type**: Python web application implemented as a modular monolith with four runtime roles:
API, workflow/recovery worker, Outbox publisher, and RocketMQ consumers

**Performance Goals**: Serve a documented 20-concurrent-user test without unbounded growth; complete
at least 95% of accepted read interactions within their configured deadline; complete safe steps in
the flagship flow within 3 minutes excluding approval wait; measure P50/P95/P99 and saturation rather
than claim an untested production QPS

**Constraints**: Fourteen calendar days; one enterprise and four roles; 2,000–5,000 documents;
external model quotas are variable; write paths require durable audit and business-effect idempotency;
all external calls use a propagated deadline; sensitive operations and notifications default to
sandbox adapters; no Kubernetes, full admin console, model training, or arbitrary tool execution

**Scale/Scope**: One polished flagship workflow; six supported intents; direct knowledge and ticket
flows; one active document collection with versioned ACL metadata; a small labeled intent/RAG set;
four process types that can be multiplied locally to demonstrate cross-process correctness

## Constitution Check

*GATE: Passed before Phase 0 research and passed again after Phase 1 design.*

| Gate | Pre-Design | Post-Design Evidence |
|---|---|---|
| Two-week, interview-ready scope | PASS | One vertical workflow, minimal UI, bounded corpus, explicit non-goals and day-by-day cut line |
| Python-first modular monolith | PASS | One `src/knowflow` package; runtime roles are entry points over shared modules, not microservices |
| MySQL business authority | PASS | Data model places durable facts and projections in MySQL; Redis/Milvus/MQ are recoverable or derived |
| Controlled model authority | PASS | Intent/plan schemas, capability registry, authorization policy, DAG compiler, and approval contract precede tools |
| ACL and least privilege | PASS | Repository methods require access context; Milvus filter is server-built; retrieval and citation are reauthorized |
| Stable side-effect identity | PASS | Operation ledger and payload hash precede replayable nodes; external adapters receive the same operation ID |
| Outbox/Inbox transaction boundaries | PASS | Event contracts define stable logical message IDs; model defines producer and consumer local transactions |
| At-least-once, non-duplicated effects | PASS | No exactly-once claim; duplicate delivery and replay are explicit contract/test cases |
| Deadline, backpressure, cancellation | PASS | Absolute deadlines, bounded queues, local/global limits, and pre/post durable-acceptance cancellation are designed |
| Evidence before claims | PASS | Test layout includes security, contract, E2E, fault, evaluation, and load evidence; reports retain context |
| Governed parallel execution | PASS | Constitution 1.1.0 restricts concurrency to `[P]`; the main agent reviews diffs/tests and owns commits, pushes, and merges |

No constitutional violations require a complexity exception.

## Phase 0: Research Decisions

The completed decisions, rationale, alternatives, and primary references are in
[research.md](research.md). All technical unknowns are resolved; no `NEEDS CLARIFICATION` item remains.

## Phase 1: Design Outputs

- [data-model.md](data-model.md) defines authoritative/derived entities, constraints, relationships,
  and state transitions.
- [contracts/openapi.yaml](contracts/openapi.yaml) defines the synchronous REST and SSE boundary.
- [contracts/events.md](contracts/events.md) defines the RocketMQ envelope, event catalog,
  delivery/compatibility rules, and consumer outcomes.
- [quickstart.md](quickstart.md) defines the planned runnable setup and validation scenarios.

## Project Structure

### Documentation (this feature)

```text
specs/001-knowflow-agent-platform/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── openapi.yaml
│   └── events.md
├── checklists/
│   └── requirements.md
└── tasks.md
```

### Source Code (repository root)

```text
pyproject.toml
uv.lock
.env.example
docker-compose.yml
alembic.ini
src/
└── knowflow/
    ├── api/
    │   ├── main.py
    │   ├── dependencies.py
    │   ├── error_handlers.py
    │   └── routes/
    ├── application/
    │   ├── auth/
    │   ├── knowledge/
    │   ├── tickets/
    │   ├── workflows/
    │   ├── approvals/
    │   └── notifications/
    ├── domain/
    │   ├── common/
    │   ├── identity/
    │   ├── knowledge/
    │   ├── tickets/
    │   ├── workflows/
    │   └── messaging/
    ├── infrastructure/
    │   ├── db/
    │   ├── redis/
    │   ├── retrieval/
    │   ├── messaging/
    │   ├── models/
    │   ├── notifications/
    │   └── observability/
    ├── workflows/
    │   ├── state.py
    │   ├── graph.py
    │   ├── planner.py
    │   ├── nodes/
    │   └── recovery.py
    ├── workers/
    │   ├── workflow.py
    │   ├── outbox.py
    │   ├── documents.py
    │   ├── notifications.py
    │   ├── sla.py
    │   └── reconciliation.py
    ├── evaluation/
    │   ├── intent.py
    │   ├── rag.py
    │   ├── workflow.py
    │   └── reporting.py
    ├── web/
    │   ├── templates/
    │   └── static/
    ├── cli.py
    └── config.py
alembic/
├── env.py
└── versions/
data/
├── seed/
└── eval/
scripts/
├── dev.ps1
├── demo.ps1
└── fault-injection.ps1
tests/
├── unit/
├── contract/
├── integration/
├── e2e/
├── fault/
├── evaluation/
└── load/
reports/
├── evaluation/
├── load/
└── fault/
```

**Structure Decision**: Use one installable `src/knowflow` package with domain, application, and
infrastructure dependency direction. API and workers share schemas and use cases but have distinct
entry points, connection pools, limits, and scaling. A minimal server-rendered UI avoids a second
frontend toolchain while still demonstrating login, chat/events, tickets, approvals, and traces.

The source tree also includes `application/audit/`, `infrastructure/operations/`, and
`infrastructure/testing/`; reports include `reports/usability/`. The API contract exposes immutable
document-version creation/detail/retry operations, resource-scoped workflow and ticket audit
timelines, hybrid automatic/manual recovery review and decisions, and notification summaries plus
dedicated delivery queries. These boundaries are tested before their implementations. Usability
evidence uses five participants and 20 scripted attempts, with 18 successful attempts required and
every in-attempt hint counted as intervention.

## Key Runtime Boundaries

| Runtime | Responsibilities | Explicit Non-Responsibilities |
|---|---|---|
| API | JWT/RBAC, object authorization, request IDs, SSE, direct reads, durable workflow acceptance | Long parsing, indefinite workflow execution, direct MQ consumption |
| Workflow worker | Plan validation, LangGraph execution/resume, tool orchestration, checkpoint reconciliation | Owning business truth or bypassing domain services |
| Outbox publisher | Lease due Outbox rows, publish stable events, update delivery attempts | Holding DB locks during network calls, providing exactly-once transport |
| Consumers | Document indexing, notifications, SLA checks, recovery commands with Inbox dedupe | Trusting event payload over current MySQL state |

## Transaction and Failure Boundaries

1. Ticket/approval/operation changes, audit entry, workflow projection update, and corresponding
   Outbox event commit together where they represent one business decision.
2. Outbox rows are leased in short transactions; RocketMQ calls occur outside those transactions.
3. Each consumer records Inbox receipt and local database effects in one transaction, then acknowledges.
4. External delivery occurs from a durable delivery record with a stable operation ID; uncertain
   timeout results enter `UNKNOWN` and reconciliation instead of blind retry.
5. LangGraph nodes treat MySQL operation results as authoritative on replay and reconstruct State
   from those results before advancing the checkpoint.

## Two-Week Cut Line

| Days | Planned demonstrable outcome |
|---|---|
| 1–2 | Repository, Compose dependencies, configuration, schema/migrations, seed users/RBAC, API skeleton |
| 3–5 | Document ingestion, ACL-filtered hybrid retrieval, citation answer, RAG evaluation seed |
| 6–7 | Six-intent catalog, structured plan/DAG validation, slot clarification, deterministic model stub |
| 8–9 | Ticket lifecycle, flagship graph, approval interrupt/resume, sandbox operations, minimal UI |
| 10–11 | Outbox publisher, RocketMQ consumers, Inbox, notification states, SLA delayed checks |
| 12 | Deadline/backpressure/cancellation, multi-worker correctness, five fault-injection scenarios |
| 13–14 | OTel/Langfuse, evaluation and Locust reports, quickstart/README, demo script/video, resume evidence |

If schedule slips, retain the flagship workflow, auth/ACL, ticket idempotency, approval recovery,
Outbox/Inbox, and one reproducible failure case. Defer broad document formats, extra notification
channels, polished administration pages, and nonessential dashboards first.

## Complexity Tracking

No constitution violations or justified complexity exceptions are present.
