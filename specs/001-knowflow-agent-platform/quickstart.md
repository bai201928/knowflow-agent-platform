# Quickstart and Validation Guide

This is the runnable acceptance contract for the completed MVP. The implementation tasks must make
these commands and outcomes true; this planning artifact does not claim that source code exists yet.

## 1. Prerequisites

- Windows 11 with PowerShell 7 and Docker Desktop, or a Linux host with Docker Compose v2
- Python 3.12 and `uv`
- At least 8 CPU threads, 16 GB RAM, and 20 GB free disk for the full local dependency profile
- An OpenAI-compatible chat/embedding endpoint for real-model tests (optional for the stub demo)
- Optional Langfuse v4 endpoint and credentials; absence must not prevent the core demo

Verify tools:

```powershell
python --version
uv --version
docker version
docker compose version
```

Expected: Python reports 3.12.x and Docker reports a reachable engine.

## 2. Configure and Install

```powershell
Copy-Item .env.example .env
uv sync --all-groups
```

For the deterministic local path, set these values in `.env`:

```text
KNOWFLOW_MODEL_MODE=stub
KNOWFLOW_NOTIFICATION_MODE=mail_capture
KNOWFLOW_OPS_MODE=sandbox
KNOWFLOW_LANGFUSE_ENABLED=false
```

Never commit `.env`. Real-model settings are added only for the separate quality run.

## 3. Start Core Dependencies

```powershell
docker compose up -d --wait mysql redis etcd minio milvus rocketmq-namesrv rocketmq-broker rocketmq-proxy mailpit
docker compose ps
```

Expected:

- MySQL, Redis 8, Milvus dependencies, RocketMQ 5.5 plus gRPC proxy, and Mailpit are healthy.
- The core profile does not require hosted observability credentials.
- No application process starts before migrations and seed data succeed.

## 4. Migrate, Seed, and Check

```powershell
uv run alembic upgrade head
uv run python -m knowflow.cli seed --profile demo
uv run python -m knowflow.cli check
```

Expected seed data:

- Four demo users representing employee, operator, approver, and administrator roles.
- Separate employee/NOC/platform teams and at least one cross-scope negative-access fixture.
- RocketMQ, Redis, and MySQL public manuals plus at least one protected document pair.
- A sandbox consumer resource and notification capture destination.
- A labeled minimal intent and knowledge evaluation set.

The seed command prints local-only login instructions; passwords are not stored in this guide.

## 5. Start the Application Runtimes

In separate terminals, or through the development launcher:

```powershell
./scripts/dev.ps1
```

Equivalent process entry points:

```powershell
uv run uvicorn knowflow.api.main:app --host 127.0.0.1 --port 8000
uv run python -m knowflow.workers.workflow
uv run python -m knowflow.workers.outbox
uv run python -m knowflow.workers.documents
uv run python -m knowflow.workers.notifications
uv run python -m knowflow.workers.sla
uv run python -m knowflow.workers.reconciliation
```

Expected:

- `http://127.0.0.1:8000/docs` exposes the contract in `contracts/openapi.yaml`.
- `http://127.0.0.1:8000/demo` exposes the small login/workflow/ticket/approval UI.
- `http://127.0.0.1:8025` exposes captured sandbox mail.
- Startup fails clearly if required schema, Redis checkpoint indexes, or MQ topics are missing.

## 6. Flagship End-to-End Scenario

### 6.1 Employee request

Log in as the seeded employee and submit:

> 查询 RocketMQ 消息积压处理手册，创建一个 P1 工单并通知值班人员；调用消费者重启工具前，需要我审批。

Expected before approval:

1. The workflow is durably accepted and exposes a stable workflow ID and event stream.
2. The plan contains knowledge query, P1 ticket creation, notification, approval, and sandbox
   operations tasks with explicit dependencies.
3. The knowledge answer cites an authorized document/version/segment.
4. Exactly one P1 ticket exists even if the browser resends the request with the same key.
5. A logical notification is captured through the durable delivery path.
6. The workflow reaches `WAITING_APPROVAL`; the sandbox action has not executed.

### 6.2 Approval and resume

Log in as the seeded approver, open the pending approval, verify normalized parameters and resource
version, and approve once. Submit the same decision a second time to exercise idempotency.

Expected after approval:

1. Both approval responses identify the same immutable decision.
2. One resume command advances the workflow; a duplicate command is a replay hit.
3. The sandbox action executes once and records one successful operation result.
4. The workflow ends `SUCCEEDED` with a summary linking its citation, ticket, notification, approval,
   sandbox action, and audit trail.

## 7. Independent Story Checks

### Knowledge and ACL

```powershell
uv run pytest tests/e2e/test_knowledge_story.py -q
```

Expected: authorized citation answers and insufficient-evidence responses pass; cross-user/document
scope access, citation opening, cache reuse, and trace leakage tests expose zero protected content.

### Ticket and concurrency

```powershell
uv run pytest tests/e2e/test_ticket_story.py tests/integration/test_ticket_concurrency.py -q
```

Expected: create/query/update works in scope; 100 repeated creates yield one ticket; simultaneous
updates from one version yield one success and explicit conflicts.

### Contract suite

```powershell
uv run pytest tests/contract -q
```

Expected: generated application OpenAPI and Pydantic event schemas remain compatible with
`contracts/openapi.yaml` and `contracts/events.md` examples.

## 8. Full Automated Test Gates

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest tests/unit tests/contract --cov=knowflow --cov-report=term-missing
uv run pytest tests/integration tests/e2e -m "not real_model" -q
```

Expected: every command exits zero. Coverage is reported as evidence, not used as a substitute for
the required state/security assertions.

## 9. Mandatory Fault-Injection Evidence

Run the deterministic fault suite:

```powershell
./scripts/fault-injection.ps1 -Scenario all
```

Equivalent targeted tests:

```powershell
uv run pytest tests/fault/test_commit_before_checkpoint.py -q
uv run pytest tests/fault/test_commit_before_mq_ack.py -q
uv run pytest tests/fault/test_duplicate_approval_resume.py -q
uv run pytest tests/fault/test_stale_missing_checkpoint.py -q
uv run pytest tests/fault/test_concurrent_stale_update.py -q
```

Expected invariants:

- One ticket and one logical Outbox event after a post-commit worker exit and repeated resume.
- One consumer-side business effect after commit-before-ack redelivery.
- One approval decision, resume ownership, and sandbox operation under duplicate/concurrent resumes.
- Stale/missing checkpoints reconcile from MySQL; uncertain high-risk work becomes `NEEDS_REVIEW`.
- One same-version ticket update wins and stale writers cannot overwrite it.
- Reports, database snapshots, trace IDs, and relevant logs are written under `reports/fault/`.

## 10. Evaluation Runs

### Deterministic regression

```powershell
uv run python -m knowflow.evaluation.run --suite intent --dataset data/eval/intent-v1.jsonl
uv run python -m knowflow.evaluation.run --suite rag --dataset data/eval/rag-v1.jsonl
uv run python -m knowflow.evaluation.run --suite workflow --dataset data/eval/workflow-v1.jsonl
```

Expected report fields: code revision, dataset/configuration hash, model adapter and prompt versions,
sample count, per-case failures, aggregate metrics, environment, and artifact/trace links. Targets from
the spec remain labeled as targets until these reports actually satisfy them.

### Real external model

Set provider/model credentials locally, then run a bounded sample:

```powershell
$env:KNOWFLOW_MODEL_MODE='openai_compatible'
uv run python -m knowflow.evaluation.run --suite real-model --dataset data/eval/real-model-smoke-v1.jsonl --max-cases 30
```

Expected: quality, first-token/full latency, 429/timeouts, tokens, and cost are reported separately
from the deterministic platform run. Secret values and protected document text are absent from reports.

## 11. Controlled Load

```powershell
uv run locust -f tests/load/locustfile.py --headless --users 20 --spawn-rate 2 --run-time 10m --host http://127.0.0.1:8000 --csv reports/load/stub-20-users
```

Expected:

- Report identifies the deterministic model delay/error profile and request mix.
- It includes accepted throughput, rejected/pending proportions, P50/P95/P99, queue wait, oldest work,
  event-loop lag, database/Redis/MQ/model limits, and correctness counters.
- Queues remain bounded; rejected requests are not described as accepted.
- Duplicate ticket, unauthorized operation, and unauthorized citation counts remain zero.
- This run is labeled “stub-model platform load,” never “real-model throughput.”

## 12. Teardown and Reset

```powershell
./scripts/dev.ps1 -Stop
docker compose down
```

For a deliberate destructive local reset only:

```powershell
docker compose down -v
```

The destructive command must be documented as deleting local MySQL, Redis, Milvus, RocketMQ, and
Mailpit data. Test reports under `reports/` remain until the user removes them explicitly.
