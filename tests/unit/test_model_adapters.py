"""Behavior contract for deterministic and OpenAI-compatible model adapters.

These tests intentionally precede the T021 implementation.  No test in this
module is allowed to contact a real model provider or read an API key from the
environment.
"""

from __future__ import annotations

import json
import time
import traceback
from typing import Literal

import httpx
import pytest
import respx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from knowflow.infrastructure.models.adapters import (
    ChatMessage,
    ChatModelPort,
    DeterministicModelStub,
    EmbeddingPort,
    ModelAdapterError,
    OpenAICompatibleModelAdapter,
    ProviderErrorKind,
)

BASE_URL = "https://models.example/v1"
CHAT_URL = f"{BASE_URL}/chat/completions"
EMBEDDING_URL = f"{BASE_URL}/embeddings"
API_KEY = "unit-test-provider-secret"
PROVIDER_PAYLOAD_SENTINEL = "provider-payload-sentinel-never-log"


class StructuredPlan(BaseModel):
    """Small strict schema standing in for a compiled model plan."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    intent: Literal["knowledge_search", "ticket_update"]
    confidence: float = Field(ge=0, le=1)


def _plan(*, confidence: float = 0.95) -> StructuredPlan:
    return StructuredPlan(intent="knowledge_search", confidence=confidence)


def _adapter(*, timeout_seconds: float = 1.0) -> OpenAICompatibleModelAdapter:
    return OpenAICompatibleModelAdapter(
        base_url=BASE_URL,
        api_key=SecretStr(API_KEY),
        chat_model="chat-test",
        embedding_model="embedding-test",
        timeout_seconds=timeout_seconds,
    )


def _deadline(seconds: float = 2.0) -> float:
    """Return the absolute monotonic deadline required by the model ports."""

    return time.monotonic() + seconds


def _chat_envelope(content: str) -> dict[str, object]:
    return {
        "id": "chatcmpl-test",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9},
    }


def _render_exception_chain(error: BaseException) -> str:
    """Render both the public traceback and explicitly retained exception links."""

    rendered = [str(error), repr(error), "".join(traceback.format_exception(error))]
    seen = {id(error)}
    linked = error.__cause__ or error.__context__
    while linked is not None and id(linked) not in seen:
        seen.add(id(linked))
        rendered.extend((str(linked), repr(linked)))
        linked = linked.__cause__ or linked.__context__
    return "\n".join(rendered)


def test_deterministic_stub_implements_both_async_ports() -> None:
    stub = DeterministicModelStub(structured_response=_plan(), embedding_dimensions=4)

    assert isinstance(stub, ChatModelPort)
    assert isinstance(stub, EmbeddingPort)


async def test_deterministic_stub_returns_a_fresh_validated_structured_value() -> None:
    expected = _plan()
    stub = DeterministicModelStub(structured_response=expected, embedding_dimensions=4)

    first = await stub.chat_structured(
        messages=[ChatMessage(role="user", content="find the runbook")],
        response_model=StructuredPlan,
        deadline=_deadline(),
    )
    second = await stub.chat_structured(
        messages=[ChatMessage(role="user", content="the prompt must not change the fixture")],
        response_model=StructuredPlan,
        deadline=_deadline(),
    )

    assert first == expected
    assert second == expected
    assert first is not expected
    assert second is not first


async def test_deterministic_stub_embeddings_are_stable_bounded_and_ordered() -> None:
    stub = DeterministicModelStub(structured_response=_plan(), embedding_dimensions=4)

    first = await stub.embed(texts=["alpha", "beta", "alpha"], deadline=_deadline())
    second = await stub.embed(texts=["alpha", "beta", "alpha"], deadline=_deadline())

    assert first == second
    assert len(first) == 3
    assert first[0] == first[2]
    assert first[0] != first[1]
    assert all(len(vector) == 4 for vector in first)
    assert all(-1.0 <= value <= 1.0 for vector in first for value in vector)


@respx.mock
async def test_openai_chat_sends_schema_request_and_returns_validated_model() -> None:
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json=_chat_envelope(_plan().model_dump_json()))
    )

    result = await _adapter().chat_structured(
        messages=[
            ChatMessage(role="system", content="Return a plan."),
            ChatMessage(role="user", content="Find the incident runbook."),
        ],
        response_model=StructuredPlan,
        deadline=_deadline(),
    )

    assert result == _plan()
    request = route.calls.last.request
    body = json.loads(request.content)
    assert body["model"] == "chat-test"
    assert body["messages"] == [
        {"role": "system", "content": "Return a plan."},
        {"role": "user", "content": "Find the incident runbook."},
    ]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["name"] == "StructuredPlan"
    assert "api_key" not in body
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"


@respx.mock
async def test_openai_embeddings_restore_provider_results_to_input_order() -> None:
    route = respx.post(EMBEDDING_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.3, 0.4]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ]
            },
        )
    )

    result = await _adapter().embed(texts=["first", "second"], deadline=_deadline())

    assert result == [[0.1, 0.2], [0.3, 0.4]]
    body = json.loads(route.calls.last.request.content)
    assert body == {"input": ["first", "second"], "model": "embedding-test"}


@respx.mock
async def test_expired_deadline_fails_without_contacting_provider() -> None:
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json=_chat_envelope(_plan().model_dump_json()))
    )

    with pytest.raises(ModelAdapterError) as caught:
        await _adapter().chat_structured(
            messages=[ChatMessage(role="user", content="too late")],
            response_model=StructuredPlan,
            deadline=time.monotonic() - 0.001,
        )

    assert caught.value.kind is ProviderErrorKind.DEADLINE_EXCEEDED
    assert caught.value.retryable is False
    assert route.called is False


@respx.mock
async def test_http_timeout_is_normalized_as_retryable() -> None:
    provider_request = httpx.Request(
        "POST",
        CHAT_URL,
        headers={"Authorization": f"Bearer {API_KEY}"},
    )
    respx.post(CHAT_URL).mock(
        side_effect=httpx.ReadTimeout(PROVIDER_PAYLOAD_SENTINEL, request=provider_request)
    )

    with pytest.raises(ModelAdapterError) as caught:
        await _adapter(timeout_seconds=0.01).chat_structured(
            messages=[ChatMessage(role="user", content="slow request")],
            response_model=StructuredPlan,
            deadline=_deadline(),
        )

    assert caught.value.kind is ProviderErrorKind.TIMEOUT
    assert caught.value.retryable is True
    assert caught.value.status_code is None
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    rendered_chain = _render_exception_chain(caught.value)
    assert API_KEY not in rendered_chain
    assert PROVIDER_PAYLOAD_SENTINEL not in rendered_chain
    assert "Authorization" not in rendered_chain


@pytest.mark.parametrize(
    ("status_code", "expected_kind"),
    [
        (429, ProviderErrorKind.RATE_LIMITED),
        (500, ProviderErrorKind.UPSTREAM_UNAVAILABLE),
        (503, ProviderErrorKind.UPSTREAM_UNAVAILABLE),
    ],
)
@respx.mock
async def test_retryable_provider_statuses_are_normalized(
    status_code: int, expected_kind: ProviderErrorKind
) -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(status_code, json={"error": {"message": "provider detail"}})
    )

    with pytest.raises(ModelAdapterError) as caught:
        await _adapter().chat_structured(
            messages=[ChatMessage(role="user", content="retryable")],
            response_model=StructuredPlan,
            deadline=_deadline(),
        )

    assert caught.value.kind is expected_kind
    assert caught.value.retryable is True
    assert caught.value.status_code == status_code


@respx.mock
async def test_non_json_chat_content_is_a_non_retryable_invalid_response() -> None:
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_chat_envelope("not-json")))

    with pytest.raises(ModelAdapterError) as caught:
        await _adapter().chat_structured(
            messages=[ChatMessage(role="user", content="malformed")],
            response_model=StructuredPlan,
            deadline=_deadline(),
        )

    assert caught.value.kind is ProviderErrorKind.INVALID_RESPONSE
    assert caught.value.retryable is False
    assert caught.value.status_code == 200


@respx.mock
async def test_schema_invalid_chat_content_is_a_non_retryable_invalid_response() -> None:
    invalid_plan = json.dumps(
        {
            "schema_version": "1",
            "intent": PROVIDER_PAYLOAD_SENTINEL,
            "confidence": 1.5,
        }
    )
    respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=_chat_envelope(invalid_plan)))

    with pytest.raises(ModelAdapterError) as caught:
        await _adapter().chat_structured(
            messages=[ChatMessage(role="user", content="invalid schema")],
            response_model=StructuredPlan,
            deadline=_deadline(),
        )

    assert caught.value.kind is ProviderErrorKind.INVALID_RESPONSE
    assert caught.value.retryable is False
    assert caught.value.status_code == 200
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    rendered_chain = _render_exception_chain(caught.value)
    assert PROVIDER_PAYLOAD_SENTINEL not in rendered_chain


@respx.mock
async def test_authentication_failure_never_leaks_api_key_or_authorization_header() -> None:
    respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            401,
            json={"error": {"message": f"rejected Bearer {API_KEY}"}},
        )
    )

    with pytest.raises(ModelAdapterError) as caught:
        await _adapter().chat_structured(
            messages=[ChatMessage(role="user", content="authenticate")],
            response_model=StructuredPlan,
            deadline=_deadline(),
        )

    error = caught.value
    rendered = f"{error!s} {error!r}"
    assert error.kind is ProviderErrorKind.AUTHENTICATION
    assert error.retryable is False
    assert error.status_code == 401
    assert API_KEY not in rendered
    assert "Authorization" not in rendered
    assert "Bearer" not in rendered
