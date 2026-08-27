# Quickstart V2 — Interview Demo Contract

V2 adds a scenario-oriented teaching runner. The full production stack is not required to understand the control flow.

## Scenario Runner

```bash
python demo_runner.py --list
python demo_runner.py payment_release_incident
python demo_runner.py mq_backlog_remediation
python demo_runner.py knowledge_query
python demo_runner.py multi_intent_ticket
python demo_runner.py query_status
python demo_runner.py conversation_thread
```

Each run prints:

1. User query;
2. raw LLM `IntentRecognitionResult`;
3. deterministic slot check;
4. Intent -> default ExecutionChannel mapping;
5. compiled `IntentExecutionPlan` / Task DAG;
6. Coordinator directives or an explicit “Coordinator skipped” message;
7. interview notes.

## Expected Observations

### knowledge_query

Only `DIRECT_READ`. The output MUST explicitly state that Coordinator Graph is skipped.

### query_status

Only direct business query. The output MUST explicitly state that Coordinator Graph is skipped.

### payment_release_incident

Metrics/log intents are absorbed as requested read capabilities. Rollback/Verify/post-recovery notification become one Coordinator invocation with stage switches ending at `NOTIFY`.

### multi_intent_ticket

Expected DAG:

```text
T1 SEARCH_KNOWLEDGE
  ├─ T2 CREATE_TICKET -> T3 SEND_NOTIFICATION
  └─ T4 INVESTIGATE_INCIDENT -> T5 RESTART_SERVICE if consumer_abnormal
```

T4/T5 share one coordinator group. The fixed Graph evaluates the condition from structured diagnosis facts before entering write remediation.

### conversation_thread

At least four Turns share one Thread. Each turn prints a new Run lineage; later Turns refine or extend previous goals instead of rewriting history.

## Reliability Study Scripts

The existing response-loss/MQ-redelivery, Approval drift, Redis checkpoint-loss and stale-fencing demonstrations remain part of the learning package. V2 intentionally does not add more failure scenarios.
