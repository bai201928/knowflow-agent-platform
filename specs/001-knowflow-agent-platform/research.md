# Phase 0 Research: KnowFlow Reliable Agent Platform

All decisions below are resolved for the two-week MVP. Versions are pinned in the future lockfile;
the plan names major compatibility lines rather than claiming that an untested latest release works.

## Decision 1: Modular Monolith With Separate Runtime Roles

**Decision**: Use one Python package and one schema, with separately launched API, workflow worker,
Outbox publisher, and RocketMQ consumer processes.

**Rationale**: This preserves clear transaction and domain boundaries while keeping a two-week build
debuggable. The processes can demonstrate multi-process concurrency without adding network contracts
between premature microservices.

**Alternatives considered**:

- Multiple microservices: rejected because distributed deployment and API versioning add no MVP value.
- One API process with in-memory background tasks: rejected because process exit would lose accepted work.

## Decision 2: FastAPI Async I/O and Native SSE

**Decision**: Use FastAPI async routes for non-blocking I/O and its native `fastapi.sse`
`EventSourceResponse` for workflow events. Keep CPU-heavy parsing and synchronous RocketMQ work out of
the API event loop.

**Rationale**: The API workload is dominated by model, database, Redis, and retrieval waits. Native
SSE provides typed JSON events, event IDs, and reconnect hints without adding a second real-time stack.
FastAPI's concurrency guidance distinguishes awaited I/O from CPU-bound work, so process isolation is
part of the design rather than assuming `async def` makes blocking libraries non-blocking.

**Alternatives considered**:

- WebSockets: rejected because the MVP primarily needs server-to-client progress and resumable IDs.
- Long polling: simpler but creates more request churn and a poorer streamed demo.
- A separate SPA: rejected to avoid a second build toolchain; Jinja2 and small static scripts suffice.

**Primary references**: [FastAPI concurrency](https://fastapi.tiangolo.com/async/),
[FastAPI SSE](https://fastapi.tiangolo.com/tutorial/server-sent-events/)

## Decision 3: LangGraph With Redis Checkpoints, MySQL Projection

**Decision**: Use LangGraph for the validated task graph, `interrupt()`/`Command(resume=...)`, and
`AsyncRedisSaver` from `langgraph-checkpoint-redis`. Initialize Redis indexes during setup. Persist a
separate MySQL workflow projection and operation ledger for business recovery.

**Rationale**: Checkpointers are thread-scoped graph snapshots for continuity and fault tolerance,
not the business ledger. Redis 8 includes the JSON/search modules required by the Redis checkpointer.
LangGraph restarts an interrupted node on resume, so every action before and after an interrupt must
use stable identities and idempotent domain services.

**Alternatives considered**:

- In-memory saver: rejected because process restart destroys the demo's central recovery behavior.
- Community MySQL checkpointer: rejected because it blurs business tables and execution snapshots and
  is not the selected maintained integration.
- Redis as sole business source: rejected because snapshot loss or eviction must not erase tickets or approvals.

**Primary references**: [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence),
[LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts),
[Redis LangGraph checkpointer](https://github.com/redis-developer/langgraph-redis)

## Decision 4: MySQL 8.4 With SQLAlchemy Async and Alembic

**Decision**: Use SQLAlchemy 2's asyncio layer with the `asyncmy` dialect and Alembic migrations.
Use explicit transactions, repository ports that require `AccessContext`, optimistic `version`
columns, uniqueness constraints, and short `FOR UPDATE SKIP LOCKED` leases for queue-like tables.

**Rationale**: The async dialect prevents normal database waits from blocking FastAPI's event loop,
while SQLAlchemy/Alembic keep mappings and schema evolution explicit. MySQL 8 provides the transaction,
conditional update, unique constraint, and locking features required by the reliability design.

**Alternatives considered**:

- Synchronous ORM inside the API: rejected because it requires pervasive thread offloading.
- Raw SQL everywhere: rejected because two-week schema evolution and cross-cutting access rules become brittle.
- PostgreSQL: technically suitable, but rejected to preserve the user's chosen MySQL interview scope.

**Primary reference**: [SQLAlchemy asyncio](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)

## Decision 5: Official RocketMQ 5.x gRPC Python Client in Dedicated Processes

**Decision**: Use Apache's `rocketmq-clients` Python binding, packaged as
`rocketmq-python-client`, against the RocketMQ 5.x gRPC proxy. Publisher and consumer SDK calls live
in dedicated processes; the API writes Outbox rows instead of publishing directly.

**Rationale**: The Apache repository identifies these bindings as the 5.x, Protocol Buffers/gRPC
replacement for 4.x clients and lists Python support for standard, FIFO, delay, transaction messages,
and consumers. Isolating the SDK prevents synchronous work from blocking API requests and makes crash
windows testable.

**Alternatives considered**:

- Legacy `apache/rocketmq-client-python`: rejected because it targets the older remoting client line.
- Broker calls inside request transactions: rejected because database locks would span network I/O.
- RocketMQ transaction messages plus Outbox: rejected as redundant complexity for the MVP.

**Primary references**: [Apache RocketMQ clients](https://github.com/apache/rocketmq-clients),
[RocketMQ gRPC SDK overview](https://rocketmq.apache.org/docs/sdk/01overview/)

## Decision 6: MySQL Outbox/Inbox and Explicit At-Least-Once Semantics

**Decision**: Commit business facts and Outbox records together. Lease Outbox rows with owner and
expiry, publish a stable logical `message_id`, and let consumers commit Inbox deduplication plus local
effects before acknowledgement. Keep external deliveries behind a durable delivery state machine.

**Rationale**: A publisher can crash after broker acceptance but before marking `SENT`, and a
consumer can crash after database commit but before acknowledgement. Stable identities and local
transactions make these unavoidable duplicates safe and observable.

**Alternatives considered**:

- Claim broker exactly-once: rejected because it does not atomically include arbitrary MySQL and
  external notification effects.
- Redis-only deduplication: rejected because TTL, eviction, and non-atomic MySQL writes can reintroduce effects.

## Decision 7: Milvus Dense + Native BM25 Hybrid Retrieval With ACL Filtering

**Decision**: Store dense vectors, raw text, BM25 sparse output, document/version IDs, section
locations, and compact ACL metadata in one Milvus collection. Run server-built scalar filtering
before dense and BM25 searches, fuse with RRF, cap evidence, then reauthorize results and citation reads.

**Rationale**: Dense search covers semantic paraphrases; BM25 covers exact operational terms. Milvus
supports native BM25, hybrid search, reranking, and metadata filtering before ANN. Pre-filtering
prevents unauthorized chunks from occupying top-K or reaching the model/traces.

**Alternatives considered**:

- Dense-only retrieval: rejected because product codes and exact error text are important.
- Post-retrieval ACL only: rejected because it degrades recall and exposes unauthorized candidates internally.
- Separate Elasticsearch: rejected because a second search platform is unnecessary for 2,000–5,000 documents.

**Primary references**: [Milvus search overview](https://milvus.io/docs/overview.md),
[BM25 function](https://milvus.io/docs/bm25-function.md),
[filtered search](https://milvus.io/docs/filtered-search.md)

## Decision 8: Provider Adapters and Structured Planning

**Decision**: Define async chat and embedding ports with an OpenAI-compatible implementation and
deterministic stub. The model returns versioned Pydantic schemas for intents, slots, dependencies,
risk, and evidence-linked answers. A deterministic compiler validates the capability catalog, DAG,
permissions, resource versions, and approval policy before LangGraph receives a plan.

**Rationale**: Qwen and DeepSeek-compatible endpoints can vary without leaking provider concerns
into the domain. Schema validation and a fixed capability registry limit probabilistic model authority.

**Alternatives considered**:

- Model-selected arbitrary functions: rejected as unsafe and difficult to test.
- One prompt containing every tool: rejected because it worsens ambiguity, token cost, and injection surface.
- Fine-tuned classifier: rejected by the two-week scope; retrieval plus structured model output is measurable sooner.

## Decision 9: Local JWT Authentication Plus Server-Enforced RBAC/ABAC

**Decision**: Seed demo accounts with Argon2id password hashes and short-lived signed JWTs. Load the
account, role, team, session, and ACL version from server-controlled state on each protected request.
Repository and application services require `AccessContext`; `thread_id`, ticket ID, and citation ID
are never authorization credentials.

**Rationale**: This demonstrates realistic identity and object authorization without spending the
MVP on an external identity provider. It can later be replaced behind the same authentication port.

**Alternatives considered**:

- Client-provided user IDs: rejected because they are trivially forgeable.
- External SSO in v1: deferred because it does not improve the core interview story.
- RBAC alone: rejected because ticket, workflow, and document access also depend on ownership/team/scope.

## Decision 10: Deadlines, Bounded Admission, and Correct Cancellation

**Decision**: Create one monotonic absolute deadline at ingress and pass remaining budget to model,
retrieval, database, and tools. Use local semaphores for process resources and Redis token buckets or
leases for cross-process user/provider limits. Never enqueue unbounded coroutines. Cancel pure reads
on disconnect; after durable acceptance, expose explicit cancellation as a versioned workflow command.

**Rationale**: Independent per-layer timeouts multiply latency, and in-memory synchronization does
not constrain multiple workers. The chosen policy makes overload visible as rejected, pending, or
degraded work without losing accepted business commands.

**Alternatives considered**:

- A timeout of the same length at every layer: rejected because worst-case latency accumulates.
- In-memory semaphore as a global limit: rejected because Uvicorn workers do not share memory.
- `asyncio.create_task` for accepted work: rejected because process exit loses the task.

## Decision 11: OpenTelemetry Foundation With Langfuse for LLM Evidence

**Decision**: Emit OpenTelemetry traces and metrics with IDs propagated through API, workflows,
Outbox, and message links. Use structured JSON logs with trace/workflow/operation/message IDs. Send
LLM-focused observations and scores through Langfuse Python v4, with prompt/document content redacted
or disabled by default and local no-op export when credentials are absent.

**Rationale**: OTel gives vendor-neutral cross-component signals; Langfuse adds model/token/prompt and
evaluation views. Current Langfuse Python v4 is OTel-based, so one trace context avoids competing
instrumentation models.

**Alternatives considered**:

- Langfuse for all infrastructure spans: rejected because it adds noise/cost and complicates redaction.
- Plain logs only: rejected because async queue/replay paths need causal trace links and timing.
- Mandatory self-hosted Langfuse in the core Compose profile: rejected because its supporting stack
  is too heavy for the default two-week demo; it remains an optional profile or hosted endpoint.

**Primary references**: [OpenTelemetry Python](https://opentelemetry.io/docs/languages/python/),
[Langfuse compatibility](https://langfuse.com/docs/compatibility)

## Decision 12: Layered Test and Evaluation Evidence

**Decision**: Use unit and property tests for validators/state machines; contract tests for OpenAPI,
events, and adapters; Docker-backed integration tests for MySQL/Redis/Milvus/RocketMQ; E2E tests per
story; deterministic failure hooks at commit/ack/checkpoint boundaries; fixed intent/RAG/workflow
datasets; Locust load with a deterministic model; and a separate small real-model evaluation.

**Rationale**: Each metric or reliability claim needs a reproducible artifact and a test that can
identify whether failure came from retrieval, model behavior, orchestration, or infrastructure.

**Alternatives considered**:

- Real-model load testing only: rejected because provider quotas and network variance hide platform capacity.
- LLM judge as the sole quality signal: rejected because safety, citations, and deterministic facts require hard checks.
- Manual failure demos only: rejected because replay windows must remain regression-tested.
