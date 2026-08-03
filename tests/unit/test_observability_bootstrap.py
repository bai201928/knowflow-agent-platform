"""T024 behavior contract for safe, local-first observability bootstrap.

The implementation must remain inert by default: these tests never permit an
exporter or a real network call unless an exporter is explicitly configured.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator

import pytest
import structlog
from opentelemetry import context as otel_context
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NoOpTracerProvider, SpanContext, TraceFlags, set_span_in_context
from opentelemetry.trace.span import NonRecordingSpan
from pydantic import SecretStr

from knowflow.infrastructure.observability.bootstrap import (
    ObservabilityConfig,
    bootstrap_observability,
    configure_structured_logging,
    redact,
    reset_observability_for_testing,
    safe_span_link,
)

REDACTED = "[REDACTED]"
CYCLE = "[CYCLE]"
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
TRACE_ID = int("4bf92f3577b34da6a3ce929d0e0e4736", 16)
SPAN_ID = int("00f067aa0ba902b7", 16)


@pytest.fixture(autouse=True)
def _isolated_observability_runtime() -> Iterator[None]:
    reset_observability_for_testing()
    structlog.reset_defaults()
    yield
    reset_observability_for_testing()
    structlog.reset_defaults()


def test_redact_recursively_sanitizes_secrets_keys_urls_and_long_values() -> None:
    source = {
        "identity": {
            "display_name": "Ada",
            "password": "password-value-must-not-appear",
            "API-Key": SecretStr("api-key-value-must-not-appear"),
        },
        "headers": {"Authorization": "Bearer authorization-value-must-not-appear"},
        "items": [
            {"access_token": "token-value-must-not-appear"},
            "https://user:pass@example.test/path?api_key=url-secret&safe=visible#fragment",
        ],
        "description": "x" * 80,
    }

    result = redact(source, max_string_length=32)
    rendered = json.dumps(result)

    assert result["identity"]["display_name"] == "Ada"
    assert result["identity"]["password"] == REDACTED
    assert result["identity"]["API-Key"] == REDACTED
    assert result["headers"]["Authorization"] == REDACTED
    assert result["items"][0]["access_token"] == REDACTED
    assert "safe=visible" in result["items"][1]
    assert REDACTED in result["items"][1]
    assert len(result["description"]) <= 32
    assert result["description"].endswith("[TRUNCATED]")
    for secret in (
        "password-value-must-not-appear",
        "api-key-value-must-not-appear",
        "authorization-value-must-not-appear",
        "token-value-must-not-appear",
        "url-secret",
        "user:pass",
    ):
        assert secret not in rendered


def test_redact_recognizes_affixed_sensitive_keys_without_redacting_business_keys() -> None:
    source = {
        "X-API-Key": "prefixed-api-key-secret",
        "db-PASSWORD": "prefixed-password-secret",
        "client_secret_value": "suffixed-client-secret",
        "APIKEY": "compact-api-key-secret",
        "API_TOKEN": "compact-api-token-secret",
        "CLIENTSECRET": "compact-client-secret",
        "ACCESSTOKEN": "compact-access-token-secret",
    }

    result = redact(source)

    assert result["X-API-Key"] == REDACTED
    assert result["db-PASSWORD"] == REDACTED
    assert result["client_secret_value"] == REDACTED
    assert result["APIKEY"] == REDACTED
    assert result["API_TOKEN"] == REDACTED
    assert result["CLIENTSECRET"] == REDACTED
    assert result["ACCESSTOKEN"] == REDACTED
    rendered = json.dumps(result)
    assert "prefixed-api-key-secret" not in rendered
    assert "prefixed-password-secret" not in rendered
    assert "suffixed-client-secret" not in rendered
    assert "compact-api-key-secret" not in rendered
    assert "compact-api-token-secret" not in rendered
    assert "compact-client-secret" not in rendered
    assert "compact-access-token-secret" not in rendered


def test_redact_does_not_treat_business_key_words_as_credentials() -> None:
    source = {
        "monkey": "ordinary-monkey-value",
        "key_id": "ordinary-key-id",
    }

    assert redact(source) == source


def test_redact_exception_never_exposes_str_repr_or_args_secrets() -> None:
    password = "exception-password-sentinel"
    token = "exception-token-sentinel"
    api_key = "exception-api-key-sentinel"
    error = RuntimeError(f"password={password} token={token} api_key={api_key}")

    sanitized = redact(error)
    rendered = json.dumps(sanitized)

    assert password not in rendered
    assert token not in rendered
    assert api_key not in rendered
    assert "RuntimeError" in rendered or REDACTED in rendered

    output = io.StringIO()
    configure_structured_logging(stream=output)
    structlog.get_logger().error("failed-operation", exception=error)
    log_record = output.getvalue()
    assert password not in log_record
    assert token not in log_record
    assert api_key not in log_record
    assert "RuntimeError" in log_record or REDACTED in log_record


def test_redact_sanitizes_credential_assignments_in_strings_repr_and_args() -> None:
    password = "string-password-sentinel"
    token = "string-token-sentinel"
    api_key = "string-api-key-sentinel"
    representations: list[object] = [
        f"password={password}",
        f"RuntimeError('token: {token}')",
        f"ProviderError(\"api_key='{api_key}'\")",
        (f"password={password}", f"token: {token}"),
        [f"api_key='{api_key}'", "retry is safe"],
    ]

    for representation in representations:
        rendered = json.dumps(redact(representation))
        assert password not in rendered
        assert token not in rendered
        assert api_key not in rendered
        assert REDACTED in rendered

    output = io.StringIO()
    configure_structured_logging(stream=output)
    structlog.get_logger().error(
        "converted-exception",
        exception_str=f"password={password}",
        exception_repr=f"RuntimeError('token: {token}')",
        exception_args=(f"api_key='{api_key}'",),
    )
    log_record = output.getvalue()
    assert password not in log_record
    assert token not in log_record
    assert api_key not in log_record


def test_redact_preserves_plain_text_that_only_mentions_credential_fields() -> None:
    ordinary = "token count: 4; password policy enabled; api_key field is optional"

    assert redact(ordinary) == ordinary


def test_redact_replaces_self_referential_and_mixed_cycles_with_stable_marker() -> None:
    cyclic_dict: dict[str, object] = {}
    cyclic_dict["self"] = cyclic_dict
    cyclic_list: list[object] = []
    cyclic_list.append(cyclic_list)
    mixed: dict[str, object] = {}
    mixed_branch: list[object] = [mixed]
    mixed["branch"] = mixed_branch

    assert redact(cyclic_dict) == {"self": CYCLE}
    assert redact(cyclic_list) == [CYCLE]
    expected_mixed = {"branch": [CYCLE]}
    assert redact(mixed) == expected_mixed
    assert redact(mixed) == expected_mixed


def test_structlog_emits_json_correlation_ids_without_sensitive_payloads() -> None:
    output = io.StringIO()
    configure_structured_logging(stream=output)

    structlog.get_logger().info(
        "workflow-step",
        request_id="request-1",
        workflow_id="workflow-1",
        plan_id="plan-1",
        task_id="task-1",
        operation_id="operation-1",
        message_id="message-1",
        payload={
            "prompt": "payload-content-must-not-appear",
            "Authorization": "Bearer log-secret-must-not-appear",
        },
    )

    event = json.loads(output.getvalue())
    assert event["event"] == "workflow-step"
    assert event["request_id"] == "request-1"
    assert event["workflow_id"] == "workflow-1"
    assert event["plan_id"] == "plan-1"
    assert event["task_id"] == "task-1"
    assert event["operation_id"] == "operation-1"
    assert event["message_id"] == "message-1"
    assert event["payload"] == REDACTED
    rendered = output.getvalue()
    assert "payload-content-must-not-appear" not in rendered
    assert "log-secret-must-not-appear" not in rendered


def test_local_default_uses_noop_providers_and_never_constructs_exporters() -> None:
    def forbidden_factory(*_args: object, **_kwargs: object) -> object:
        pytest.fail("local no-op bootstrap attempted to construct an exporter")

    runtime = bootstrap_observability(
        ObservabilityConfig(environment="local", export_enabled=False),
        span_exporter_factory=forbidden_factory,
        metric_reader_factory=forbidden_factory,
    )

    assert isinstance(runtime.tracer_provider, NoOpTracerProvider)
    assert isinstance(runtime.meter_provider, NoOpMeterProvider)
    assert runtime.export_enabled is False


def test_explicit_export_configuration_enables_sdk_providers_without_network() -> None:
    calls: list[tuple[str, str]] = []
    span_exporter = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()

    def span_factory(endpoint: str) -> InMemorySpanExporter:
        calls.append(("trace", endpoint))
        return span_exporter

    def metric_factory(endpoint: str) -> InMemoryMetricReader:
        calls.append(("metric", endpoint))
        return metric_reader

    runtime = bootstrap_observability(
        ObservabilityConfig(
            environment="test",
            export_enabled=True,
            otlp_endpoint="http://collector.invalid:4318",
        ),
        span_exporter_factory=span_factory,
        metric_reader_factory=metric_factory,
    )

    assert isinstance(runtime.tracer_provider, TracerProvider)
    assert isinstance(runtime.meter_provider, MeterProvider)
    assert runtime.export_enabled is True
    assert calls == [
        ("trace", "http://collector.invalid:4318"),
        ("metric", "http://collector.invalid:4318"),
    ]
    runtime.shutdown()


def test_safe_span_link_parses_traceparent_without_copying_unsafe_attributes() -> None:
    link = safe_span_link(TRACEPARENT)

    assert link is not None
    assert link.context.trace_id == TRACE_ID
    assert link.context.span_id == SPAN_ID
    assert link.context.is_remote is True
    assert not link.attributes


def test_safe_span_link_accepts_an_otel_context() -> None:
    span_context = SpanContext(
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        is_remote=True,
        trace_flags=TraceFlags.SAMPLED,
        trace_state=None,
    )
    context = set_span_in_context(NonRecordingSpan(span_context), otel_context.Context())

    link = safe_span_link(context)

    assert link is not None
    assert link.context == span_context
    assert not link.attributes


@pytest.mark.parametrize(
    "invalid_context",
    [
        None,
        "",
        "not-a-traceparent",
        "00-00000000000000000000000000000000-00f067aa0ba902b7-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01",
        TRACEPARENT.upper(),
        otel_context.Context(),
    ],
)
def test_safe_span_link_rejects_invalid_context_without_raising(
    invalid_context: str | otel_context.Context | None,
) -> None:
    assert safe_span_link(invalid_context) is None


def test_repeated_bootstrap_is_idempotent() -> None:
    config = ObservabilityConfig(environment="local", export_enabled=False)

    first = bootstrap_observability(config)
    second = bootstrap_observability(config)

    assert second is first
    assert second.tracer_provider is first.tracer_provider
    assert second.meter_provider is first.meter_provider
