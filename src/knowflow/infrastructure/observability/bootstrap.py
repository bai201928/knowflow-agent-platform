"""Safe, local-first observability configuration.

The module deliberately keeps telemetry inert unless export is explicitly
enabled.  It also centralises log redaction so application code cannot
accidentally bypass the project's no-sensitive-payload logging policy.
"""

from __future__ import annotations

import re
import sys
import threading
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any, TextIO, cast
from urllib.parse import parse_qsl, quote_plus, urlsplit, urlunsplit

import structlog
from opentelemetry import context as otel_context
from opentelemetry import metrics, trace
from opentelemetry.metrics import MeterProvider as ApiMeterProvider
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Link, NoOpTracerProvider, SpanContext, TraceFlags
from pydantic import SecretBytes, SecretStr

REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"
CYCLE = "[CYCLE]"

_SENSITIVE_KEY_SEQUENCES = frozenset(
    {
        ("access", "token"),
        ("api", "key"),
        ("authorization",),
        ("client", "secret"),
        ("content",),
        ("cookie",),
        ("document", "content"),
        ("id", "token"),
        ("input",),
        ("output",),
        ("password",),
        ("payload",),
        ("prompt",),
        ("refresh", "token"),
        ("secret",),
        ("secret", "key"),
        ("set", "cookie"),
        ("token",),
    }
)
_COMPACT_SENSITIVE_KEY_ALIASES = frozenset(
    {
        "accesstoken",
        "apikey",
        "clientsecret",
        "documentcontent",
        "idtoken",
        "refreshtoken",
        "secretkey",
        "setcookie",
    }
)
_CAMEL_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_WORD_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_KEY_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_API_KEY_PATTERN = re.compile(r"(?i)\bsk-[a-z0-9_-]{8,}")
_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"(?ix)"
    r"(?P<prefix>\b(?:"
    r"access[_-]?token|api[_-]?key|client[_-]?secret|id[_-]?token|"
    r"password|refresh[_-]?token|secret(?:[_-]?key)?|token"
    r")\b\s*[:=]\s*)"
    r"(?:"
    r"(?P<quote>['\"])(?P<quoted>.*?)(?P=quote)"
    r"|(?P<bare>[^\s,;)'\"\]\}]+)"
    r")"
)
_TRACEPARENT_PATTERN = re.compile(
    r"^(?P<version>[0-9a-f]{2})-"
    r"(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<span_id>[0-9a-f]{16})-"
    r"(?P<trace_flags>[0-9a-f]{2})$"
)


type SpanExporterFactory = Callable[[str], SpanExporter]
type MetricReaderFactory = Callable[[str], MetricReader]


@dataclass(frozen=True, slots=True)
class ObservabilityConfig:
    """Explicit observability settings with export disabled by default."""

    environment: str = "local"
    service_name: str = "knowflow"
    export_enabled: bool = False
    otlp_endpoint: str | None = None
    metric_export_interval_millis: int = 60_000

    def __post_init__(self) -> None:
        if not self.environment.strip():
            raise ValueError("environment must not be empty")
        if not self.service_name.strip():
            raise ValueError("service_name must not be empty")
        if self.export_enabled and not (self.otlp_endpoint or "").strip():
            raise ValueError("otlp_endpoint is required when export is enabled")
        if self.metric_export_interval_millis <= 0:
            raise ValueError("metric_export_interval_millis must be positive")


@dataclass(slots=True)
class ObservabilityRuntime:
    """Providers created by :func:`bootstrap_observability`."""

    tracer_provider: trace.TracerProvider
    meter_provider: ApiMeterProvider
    export_enabled: bool
    _closed: bool = field(default=False, init=False, repr=False)

    def shutdown(self) -> None:
        """Flush and stop configured SDK providers exactly once."""

        if self._closed:
            return
        self._closed = True
        for provider in (self.tracer_provider, self.meter_provider):
            shutdown = getattr(provider, "shutdown", None)
            if callable(shutdown):
                shutdown()


_runtime_lock = threading.RLock()
_runtime: ObservabilityRuntime | None = None
_runtime_config: ObservabilityConfig | None = None


def _key_tokens(key: object) -> tuple[str, ...]:
    value = _CAMEL_ACRONYM_BOUNDARY.sub(r"\1 \2", str(key))
    value = _CAMEL_WORD_BOUNDARY.sub(r"\1 \2", value)
    return tuple(_KEY_TOKEN_PATTERN.findall(value.casefold()))


def _is_sensitive_key(key: object) -> bool:
    tokens = _key_tokens(key)
    if any(token in _COMPACT_SENSITIVE_KEY_ALIASES for token in tokens):
        return True
    for sensitive in _SENSITIVE_KEY_SEQUENCES:
        width = len(sensitive)
        if any(
            tokens[index : index + width] == sensitive for index in range(len(tokens) - width + 1)
        ):
            return True
    return False


def _redact_credential_assignment(match: re.Match[str]) -> str:
    prefix = match.group("prefix")
    quote = match.group("quote")
    if quote is not None:
        return f"{prefix}{quote}{REDACTED}{quote}"
    return f"{prefix}{REDACTED}"


def _truncate(value: str, max_string_length: int) -> str:
    if len(value) <= max_string_length:
        return value
    if max_string_length <= len(TRUNCATED):
        return TRUNCATED[:max_string_length]
    return f"{value[: max_string_length - len(TRUNCATED)]}{TRUNCATED}"


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return value

    hostname = parsed.hostname
    if hostname is None:
        return value
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = f":{parsed.port}" if parsed.port is not None else ""
    credentials = f"{REDACTED}@" if parsed.username is not None else ""
    netloc = f"{credentials}{hostname}{port}"

    query_parts: list[str] = []
    for key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
        safe_value = REDACTED if _is_sensitive_key(key) else query_value
        query_parts.append(f"{quote_plus(key, safe='[]')}={quote_plus(safe_value, safe='[]')}")
    query = "&".join(query_parts)
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))


def _redact_string(value: str, max_string_length: int) -> str:
    sanitized = _redact_url(value)
    sanitized = _BEARER_PATTERN.sub(f"Bearer {REDACTED}", sanitized)
    sanitized = _API_KEY_PATTERN.sub(REDACTED, sanitized)
    sanitized = _CREDENTIAL_ASSIGNMENT_PATTERN.sub(_redact_credential_assignment, sanitized)
    if sanitized != value and value.casefold().startswith(("http://", "https://")):
        return sanitized
    return _truncate(sanitized, max_string_length)


def _redact(value: object, *, max_string_length: int, active_ids: set[int]) -> object:
    if isinstance(value, (SecretStr, SecretBytes)):
        return REDACTED
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": REDACTED}
    if isinstance(value, (Mapping, tuple, list, set, frozenset)):
        identity = id(value)
        if identity in active_ids:
            return CYCLE
        active_ids.add(identity)
        try:
            if isinstance(value, Mapping):
                sanitized: dict[object, object] = {}
                for key, item in value.items():
                    if _is_sensitive_key(key):
                        sanitized[key] = REDACTED
                    else:
                        sanitized[key] = _redact(
                            item,
                            max_string_length=max_string_length,
                            active_ids=active_ids,
                        )
                return sanitized
            if isinstance(value, tuple):
                return tuple(
                    _redact(item, max_string_length=max_string_length, active_ids=active_ids)
                    for item in value
                )
            return [
                _redact(item, max_string_length=max_string_length, active_ids=active_ids)
                for item in value
            ]
        finally:
            active_ids.remove(identity)
    if isinstance(value, str):
        return _redact_string(value, max_string_length)
    if isinstance(value, bytes):
        return REDACTED
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_string(str(value), max_string_length)


def redact(value: object, *, max_string_length: int = 2_048) -> object:
    """Recursively return a JSON-safe value with secrets and cycles removed."""

    if max_string_length <= 0:
        raise ValueError("max_string_length must be positive")
    return _redact(value, max_string_length=max_string_length, active_ids=set())


def _redact_event(
    _logger: object,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> Mapping[str, Any]:
    sanitized = redact(event_dict)
    return cast(Mapping[str, Any], sanitized)


def configure_structured_logging(*, stream: TextIO | None = None) -> None:
    """Configure deterministic JSON logs with redaction as the final data processor."""

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_event,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stdout),
        wrapper_class=structlog.make_filtering_bound_logger(0),
        cache_logger_on_first_use=False,
    )


def _default_span_exporter(endpoint: str) -> SpanExporter:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=endpoint)


def _default_metric_reader(endpoint: str, interval_millis: int) -> MetricReader:
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

    exporter = OTLPMetricExporter(endpoint=endpoint)
    return PeriodicExportingMetricReader(
        exporter,
        export_interval_millis=interval_millis,
    )


def bootstrap_observability(
    config: ObservabilityConfig,
    *,
    span_exporter_factory: SpanExporterFactory | None = None,
    metric_reader_factory: MetricReaderFactory | None = None,
) -> ObservabilityRuntime:
    """Create and register local no-op or explicitly enabled SDK providers.

    Exporter modules and factories are never touched on the disabled path.
    Repeated calls with the same configuration return the original runtime;
    conflicting reconfiguration is rejected to avoid split telemetry state.
    """

    global _runtime, _runtime_config

    with _runtime_lock:
        if _runtime is not None:
            if config != _runtime_config:
                raise RuntimeError("observability is already configured with different settings")
            return _runtime

        configure_structured_logging()
        if not config.export_enabled:
            tracer_provider: trace.TracerProvider = NoOpTracerProvider()
            meter_provider: ApiMeterProvider = NoOpMeterProvider()
        else:
            endpoint = cast(str, config.otlp_endpoint)
            span_factory = span_exporter_factory or _default_span_exporter
            metric_factory = metric_reader_factory or (
                lambda configured_endpoint: _default_metric_reader(
                    configured_endpoint,
                    config.metric_export_interval_millis,
                )
            )
            resource = Resource.create(
                {
                    "service.name": config.service_name,
                    "deployment.environment.name": config.environment,
                }
            )
            sdk_tracer_provider = TracerProvider(resource=resource, shutdown_on_exit=False)
            sdk_tracer_provider.add_span_processor(SimpleSpanProcessor(span_factory(endpoint)))
            sdk_meter_provider = MeterProvider(
                metric_readers=[metric_factory(endpoint)],
                resource=resource,
                shutdown_on_exit=False,
            )
            tracer_provider = sdk_tracer_provider
            meter_provider = sdk_meter_provider

        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
        _runtime = ObservabilityRuntime(
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
            export_enabled=config.export_enabled,
        )
        _runtime_config = config
        return _runtime


def _span_context_from_traceparent(value: str) -> SpanContext | None:
    match = _TRACEPARENT_PATTERN.fullmatch(value)
    if match is None or match.group("version") == "ff":
        return None
    trace_id = int(match.group("trace_id"), 16)
    span_id = int(match.group("span_id"), 16)
    if trace_id == 0 or span_id == 0:
        return None
    return SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=True,
        trace_flags=TraceFlags(int(match.group("trace_flags"), 16)),
        trace_state=None,
    )


def safe_span_link(value: str | otel_context.Context | None) -> Link | None:
    """Build a metadata-free causal link from a validated context."""

    try:
        if isinstance(value, str):
            span_context = _span_context_from_traceparent(value)
        elif isinstance(value, otel_context.Context):
            span_context = trace.get_current_span(value).get_span_context()
        else:
            return None
        if span_context is None or not span_context.is_valid:
            return None
        return Link(span_context, attributes={})
    except (TypeError, ValueError):
        return None


def _reset_global_otel_providers_for_testing() -> None:
    """Reset OTel's write-once globals; only called by the explicit test helper."""

    from opentelemetry.metrics import _internal as metrics_internal
    from opentelemetry.util._once import Once

    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE = Once()
    metrics_internal._METER_PROVIDER = None
    metrics_internal._METER_PROVIDER_SET_ONCE = Once()
    metrics_internal._PROXY_METER_PROVIDER._real_meter_provider = None


def reset_observability_for_testing() -> None:
    """Shutdown and clear module/global state between isolated tests."""

    global _runtime, _runtime_config

    with _runtime_lock:
        if _runtime is not None:
            _runtime.shutdown()
        _runtime = None
        _runtime_config = None
        _reset_global_otel_providers_for_testing()
