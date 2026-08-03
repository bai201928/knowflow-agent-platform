<!--
Sync Impact Report
- Version change: 1.0.0 -> 1.1.0
- Modified sections:
  - Delivery Workflow and Quality Gates (parallel-agent governance)
  - Governance (main-agent integration accountability)
- Removed sections: none
- Follow-up TODOs: none
-->
# KnowFlow Constitution

## Core Principles

### I. Two-Week, Interview-Ready Scope
KnowFlow MUST remain a demonstrable two-week MVP centered on one complete enterprise workflow:
authenticated users ask knowledge questions, create or update tickets, send notifications, pause
for approval, and safely resume an operations action. The system MUST describe itself as
single-enterprise, multi-user software and MUST NOT claim multi-tenancy, Kubernetes-scale
production readiness, complex multi-agent autonomy, model fine-tuning, a complete admin console,
or full-corpus deployment. RAG, tickets, approvals, recovery, and reliable messaging MUST form a
real end-to-end path; notification delivery and sensitive operations MAY use clearly labeled
sandbox adapters. Scope exists to maximize credible depth and reproducible evidence within the
deadline.

### II. Python-First Boundaries and State Ownership
Python 3.12 MUST implement the end-to-end application. The first release MUST use a modular
monolith plus independently runnable API, workflow, Outbox, and consumer processes; services MUST
be split only when load, security, or ownership requires it. MySQL MUST be the authority for users,
permissions, tickets, approvals, workflow projections, operation results, audit records, Outbox,
and Inbox. Redis and LangGraph checkpoints MUST be treated as recoverable execution snapshots,
Milvus as a derived retrieval index, and RocketMQ as at-least-once transport. No cache, checkpoint,
queue, model output, or client-supplied identifier may override an authoritative business fact.

### III. Controlled Agent Authority and Least Privilege
The model MUST understand language and propose a versioned, structured task plan; deterministic
Python code MUST validate schemas, permissions, resource ownership, state transitions, DAG
structure, risk, and tool authorization before execution. The model MUST NOT generate arbitrary
SQL, shell commands, URLs, or unrestricted tool calls. Authenticated identity MUST come from the
verified server context and cannot be overridden by request slots. Knowledge ACLs MUST be applied
before retrieval and checked again for returned chunks and citation access. Missing or ambiguous
high-impact input MUST cause clarification, and every sensitive or destructive action MUST pause
for an approval bound to the exact plan version and payload hash.

### IV. Durable, Idempotent, Recoverable Execution
All side effects MUST have stable operation identities created before a replayable node executes.
MySQL uniqueness constraints, payload hashes, optimistic versions, and operation records MUST make
retries, concurrent resumes, and checkpoint replay safe. Business changes and their Outbox events
MUST commit in one transaction; consumers MUST commit Inbox deduplication and local business
effects in one transaction. The system MUST promise at-least-once delivery with non-duplicated
business effects, never transport-level exactly-once. External calls MUST use deadlines, bounded
retries only when safe, backpressure, and explicit UNKNOWN/reconciliation handling. Browser
disconnects MAY cancel pure reads but MUST NOT cancel a durably accepted workflow.

### V. Evidence Before Claims
Every behavior used in the README, demo, interview answers, or resume MUST be backed by executable
tests, traces, reports, or reproducible commands. Tests MUST cover authorization and ACL isolation,
intent and slot validation, RAG retrieval and citation quality, idempotency, approval replay,
Outbox/Inbox, concurrency, cancellation, and recovery. The five mandatory fault scenarios are:
failure after a MySQL commit, failure before MQ acknowledgement, duplicate approval/resume,
stale or missing Redis checkpoint, and concurrent updates from the same version. Stub-model load
tests and real-model quality tests MUST be reported separately. Performance and quality numbers
MUST remain marked as targets or pending measurement until produced by a versioned test run with
environment and dataset details.

## MVP Architecture and Scope Constraints

- The runtime stack MUST use Python 3.12, FastAPI, asyncio, Pydantic, LangGraph, MySQL 8.x,
  Redis 8, Milvus 2.5+, and RocketMQ 5.5 through the supported gRPC Python client.
- LangGraph persistence MUST use the Redis-maintained checkpoint integration; MySQL MUST retain a
  durable workflow projection sufficient to reconcile lost or stale checkpoints.
- Model access MUST use an OpenAI-compatible adapter so a configured Qwen or DeepSeek endpoint can
  be replaced without changing domain code. A deterministic stub MUST support repeatable tests.
- The six first-release intents are `KNOWLEDGE_QUERY`, `TICKET_CREATE`, `TICKET_QUERY`,
  `TICKET_UPDATE`, `NOTIFICATION_SEND`, and `OPS_ACTION`.
- Roles are employee, operator, approver, and administrator. Accounts, sessions, workflow threads,
  ticket objects, knowledge visibility, and tool permissions MUST be isolated and server-enforced.
- Retrieval MUST combine permission-filtered dense and lexical recall, reranking, citations, and
  evidence-insufficient refusal. Public manuals and a bounded benchmark subset are the initial data.
- Local development and the demo MUST be reproducible from documented commands and containerized
  dependencies. Secrets and credentials MUST never be committed.

## Delivery Workflow and Quality Gates

1. Each user story MUST define an independently demonstrable outcome and acceptance scenarios
   before implementation planning begins.
2. Plans MUST identify state authority, transaction boundaries, idempotency identities, failure
   behavior, security checks, deadlines, observability, and a two-week scope impact.
3. Implementation tasks MUST include automated unit, contract, integration, end-to-end, and fault
   injection tests wherever the story changes a governed invariant.
4. A story is complete only when its acceptance scenario runs from documented setup, failures are
   observable, and no real-world metric or incident is claimed without captured evidence.
5. Changes to prompts, schemas, intent catalogs, chunking, retrieval, models, or thresholds MUST be
   versioned and rerun against the relevant locked regression set.
6. Complexity beyond the modular-monolith MVP MUST include a written reason, rejected simpler
   alternative, and measurable benefit; otherwise it MUST be deferred.
7. Only tasks explicitly marked `[P]` MAY execute concurrently. Tasks that touch the same file,
   schema, transaction boundary, or depend on unfinished work MUST execute serially even if they
   otherwise appear separable.
8. The main agent MUST own the shared plan, task assignment, artifact integration, validation,
   commits, pushes, and merges. A subagent MUST NOT commit, push, or merge unless the main agent
   gives an explicit, scoped instruction.
9. Every subagent handoff MUST identify changed files, verification performed, unresolved errors,
   and assumptions. The main agent MUST inspect the diff and run relevant verification before
   marking delegated tasks complete.
10. Parallel execution MUST NOT weaken security, evidence, human-approval, or release gates.
    Runtime model selection is an execution setting and MUST NOT alter artifact precedence or
    project governance.

## Governance

This constitution is the highest-priority project rule. Specs, plans, tasks, code reviews, demos,
and resume language MUST pass its gates. Amendments require a written rationale, an updated Sync
Impact Report, a migration or remediation note for affected artifacts, and semantic versioning:
MAJOR for incompatible principle changes or removals, MINOR for new principles or materially
expanded obligations, and PATCH for non-semantic clarification. Every planning pass MUST perform a
pre-design and post-design constitution check. Every implementation review MUST verify affected
security, reliability, evidence, and scope invariants; unexplained violations block completion.

**Version**: 1.1.0 | **Ratified**: 2026-08-03 | **Last Amended**: 2026-08-03
