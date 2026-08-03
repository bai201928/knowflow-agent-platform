# RocketMQ Event Contract

## Delivery Contract

- Transport is at least once. Publishers and consumers MUST expect duplicates.
- `message_id` identifies the logical event and MUST remain unchanged across every send attempt.
- Broker-generated message IDs and send-attempt IDs are observability data, not deduplication keys.
- Producer business changes and their Outbox row commit in one MySQL transaction.
- Consumers commit `(consumer_group, message_id)` Inbox state and local business effects in one
  MySQL transaction, then acknowledge the message.
- Ordering is required only where explicitly stated. Correctness MUST also use aggregate versions.
- Unknown event types or unsupported major schema versions go to quarantine/DLQ without side effects.
- Temporary dependency errors request bounded redelivery; permanent validation errors do not loop.
- Payloads contain identifiers and minimal facts, not credentials, full prompts, or document bodies.

## Common Envelope

```json
{
  "message_id": "4a913e67-47bd-4d60-a8d6-9cd9ba80a900",
  "event_type": "ticket.created",
  "schema_version": 1,
  "occurred_at": "2026-08-03T00:00:00Z",
  "producer": "knowflow-api",
  "aggregate": {
    "type": "ticket",
    "id": "f571f013-00df-49e6-a713-d69fdc838687",
    "version": 1
  },
  "operation_id": "2d8e3d42-e56a-5ab2-b55a-d974575b7bbf",
  "workflow_id": "f787084b-1710-4295-a5e7-479808ff9f43",
  "trace": {
    "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    "tracestate": null
  },
  "payload": {}
}
```

### Envelope Validation

| Field | Rule |
|---|---|
| `message_id` | Required UUID; unique in producer Outbox and per consumer group Inbox |
| `event_type` | Required catalog value using lowercase dot notation |
| `schema_version` | Positive integer; additive compatible changes stay on the same major version |
| `occurred_at` | Required UTC RFC 3339 timestamp |
| `producer` | Required allowlisted logical process name |
| `aggregate` | Required business type, ID, and expected version where meaningful |
| `operation_id` | Required for side-effect chains; stable across retries |
| `workflow_id` | Optional only for document jobs not initiated from a workflow |
| `trace` | Trace context only; never authorization data |
| `payload` | Event-specific, size-limited, schema-validated object |

## Topics and Consumer Groups

| Topic | Events | Consumer groups |
|---|---|---|
| `knowflow.workflow.v1` | workflow command/resume/reconcile | `workflow-executors-v1` |
| `knowflow.documents.v1` | document ingestion/indexing | `document-indexers-v1` |
| `knowflow.notifications.v1` | notification delivery | `notification-senders-v1` |
| `knowflow.sla.v1` | delayed SLA checks | `sla-checkers-v1` |
| `knowflow.audit.v1` | optional derived audit/export signals | `audit-exporters-v1` |

Core MySQL audit writes do not depend on the optional audit topic.

## Event Catalog

### `workflow.command.accepted`

**Topic**: `knowflow.workflow.v1`

**Producer transaction**: Insert `workflow_commands`, update Workflow projection, and insert Outbox.

**Payload**:

```json
{
  "command_id": "approval-id:decision-version",
  "command_kind": "APPROVAL_RESUME",
  "expected_workflow_version": 8,
  "payload_hash": "sha256-hex"
}
```

**Consumer behavior**: Inbox dedupe, acquire workflow version/active-run ownership, read the durable
command and current business facts from MySQL, then execute or reject. The event payload never carries
the authoritative approval decision.

### `workflow.reconciliation.requested`

**Topic**: `knowflow.workflow.v1`

**Payload**:

```json
{
  "reason": "CHECKPOINT_MISSING",
  "expected_workflow_version": 12,
  "observed_checkpoint_id": null
}
```

**Consumer behavior**: Compare Workflow projection, Approval, Operation Record, and checkpoint. Low-risk
missing work may be reconstructed; uncertain sensitive work becomes `NEEDS_REVIEW`.

### `document.ingestion.requested`

**Topic**: `knowflow.documents.v1`

**Producer transaction**: Create Document Version in `QUEUED` and insert Outbox.

**Payload**:

```json
{
  "document_id": "uuid",
  "document_version_id": "uuid",
  "version": 1,
  "checksum_sha256": "sha256-hex",
  "source_location": "data/seed/rocketmq/manual.md",
  "media_type": "text/markdown",
  "parser_version": "parser-v1",
  "chunker_version": "chunker-v1",
  "embedding_version": "provider:model:dimensions"
}
```

**Consumer behavior**: Verify checksum and current status, parse within file limits, write authoritative
segments, build the derived index under the version ID, then atomically activate only after completeness
checks. Repeat processing converges on the same version and segment IDs.

### `document.ingestion.retry-requested`

**Topic**: `knowflow.documents.v1`

**Payload**: `document_version_id`, `expected_status=FAILED`, `failed_stage`, and requested configuration
versions. Authorization to retry is checked when the durable command is created, not trusted from MQ.

### `ticket.created`

**Topic**: `knowflow.notifications.v1`

**Producer transaction**: Insert Ticket, Ticket Event, successful Operation Record, Audit Event, and Outbox.

**Payload**:

```json
{
  "ticket_id": "uuid",
  "ticket_key": "INC-000001",
  "ticket_version": 1,
  "severity": "P1",
  "assigned_team_id": "uuid",
  "notification_policy": "ticket-created-v1"
}
```

**Consumer behavior**: Create one Notification Delivery with operation identity derived from
`message_id + channel + recipient_scope`. Actual delivery follows its own retry/UNKNOWN state machine.

### `ticket.updated`

**Topic**: `knowflow.notifications.v1`

**Payload**: `ticket_id`, `ticket_key`, `ticket_version`, changed field names, old/new status where
authorized for notification, and policy version. It contains no unrestricted ticket description.

### `approval.requested`

**Topic**: `knowflow.notifications.v1`

**Producer transaction**: Create Approval, set Workflow `WAITING_APPROVAL`, write Audit Event and Outbox.

**Payload**:

```json
{
  "approval_id": "uuid",
  "workflow_id": "uuid",
  "action_type": "consumer.restart.sandbox",
  "resource_type": "sandbox_consumer",
  "resource_id": "demo-consumer-a",
  "payload_hash": "sha256-hex",
  "requester_user_id": "uuid",
  "expires_at": "2026-08-03T01:00:00Z",
  "recipient_role": "APPROVER"
}
```

**Consumer behavior**: Notify eligible approvers but do not infer approval from delivery. The Approval
row remains authoritative.

### `approval.decided`

**Topic**: `knowflow.workflow.v1`

**Producer transaction**: Conditionally decide Approval, insert an `APPROVAL_RESUME` Workflow Command,
write Audit Event and Outbox.

**Payload**: `approval_id`, `decision_version`, `workflow_id`, and `command_id`. The workflow worker
rereads and reauthorizes the full decision from MySQL.

### `notification.delivery.requested`

**Topic**: `knowflow.notifications.v1`

**Payload**:

```json
{
  "delivery_id": "uuid",
  "channel": "MAIL_CAPTURE",
  "recipient_scope": "team:noc",
  "template": "p1-ticket-created",
  "template_version": 1,
  "data": {
    "ticket_key": "INC-000001"
  }
}
```

**Consumer behavior**: Load the durable delivery record, use its stable operation ID/provider key,
and update `DELIVERED`, `PENDING`, `UNKNOWN`, or `FAILED`. A timeout with no status query becomes UNKNOWN.

### `ticket.sla.check-requested`

**Topic**: `knowflow.sla.v1`

**Delivery**: Timed/delay message with broker delivery timestamp equal to the current SLA deadline.

**Payload**:

```json
{
  "ticket_id": "uuid",
  "expected_sla_version": 2,
  "expected_deadline": "2026-08-03T02:00:00Z",
  "escalation_level": 1
}
```

**Consumer behavior**: Treat delivery as a wake-up hint. In one local transaction, Inbox-dedupe and
read the current ticket. Apply an escalation only if unresolved, deadline passed, SLA version matches,
and the level has not already applied. Otherwise record a successful no-op.

## Retry and DLQ Classification

| Failure | Consumer result | Notes |
|---|---|---|
| Temporary database/network timeout before local commit | Retry | Backoff and preserve `message_id` |
| Process exit after local commit, before acknowledgement | Redelivery then Inbox hit | No second local effect |
| Unsupported future major schema | DLQ/quarantine | Alert; do not mutate business state |
| Malformed required field/payload hash mismatch | DLQ/quarantine | Permanent validation failure |
| Current aggregate version makes event obsolete | ACK as no-op | Record reason and metric |
| External delivery timed out with unknown outcome | ACK local event; delivery becomes `UNKNOWN` | Reconcile using stable provider key |
| Retry policy exhausted | DEAD/DLQ | Preserve error and create operator-visible recovery item |

## Compatibility Rules

1. Consumers ignore unknown optional fields.
2. Existing field meaning and type do not change within a schema version.
3. A new required field or semantic change increments `schema_version` and requires a dual-read or
   controlled migration period.
4. Producers pin a schema version per event type and contract tests validate example payloads.
5. Consumer group names include a major version so incompatible consumers cannot silently share offsets.

## Required Contract Tests

- Envelope and each event example validate against its Pydantic model.
- The same `message_id` delivered twice produces one Inbox-completed local effect.
- A commit-before-ack process exit causes redelivery and a dedupe hit.
- Outbox publish success before `SENT` update produces the same logical `message_id` on retry.
- Same event ID with a different payload hash is quarantined and audited.
- Delayed SLA events with old versions are acknowledged as no-op.
- Unsupported major schemas and malformed payloads reach the configured DLQ without poison loops.
