from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeVar, runtime_checkable

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

StructuredResult = TypeVar("StructuredResult", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """Provider-neutral chat input accepted by model adapters."""

    role: str
    content: str

    def to_provider_value(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@runtime_checkable
class ChatModelPort(Protocol):
    async def chat_structured(
        self,
        *,
        messages: Sequence[ChatMessage],
        response_model: type[StructuredResult],
        deadline: float,
    ) -> StructuredResult: ...


@runtime_checkable
class EmbeddingPort(Protocol):
    async def embed(self, *, texts: Sequence[str], deadline: float) -> list[list[float]]: ...


class ProviderErrorKind(StrEnum):
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    AUTHENTICATION = "AUTHENTICATION"
    REQUEST_REJECTED = "REQUEST_REJECTED"
    INVALID_RESPONSE = "INVALID_RESPONSE"


class ModelAdapterError(Exception):
    """Sanitized provider failure safe for logs and API error translation."""

    def __init__(
        self,
        kind: ProviderErrorKind,
        *,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        self.kind = kind
        self.retryable = retryable
        self.status_code = status_code
        status_suffix = "" if status_code is None else f" (status {status_code})"
        super().__init__(f"Model provider error: {kind.value}{status_suffix}")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(kind={self.kind!r}, retryable={self.retryable!r}, "
            f"status_code={self.status_code!r})"
        )


class DeterministicModelStub(ChatModelPort, EmbeddingPort):
    """Offline model double with repeatable structured and embedding outputs."""

    def __init__(
        self,
        *,
        structured_response: BaseModel | Mapping[str, object],
        embedding_dimensions: int = 8,
    ) -> None:
        if embedding_dimensions < 1:
            raise ValueError("embedding_dimensions must be positive")
        if isinstance(structured_response, BaseModel):
            self._structured_response = structured_response.model_dump(mode="json")
        else:
            self._structured_response = dict(structured_response)
        self._embedding_dimensions = embedding_dimensions

    async def chat_structured(
        self,
        *,
        messages: Sequence[ChatMessage],
        response_model: type[StructuredResult],
        deadline: float,
    ) -> StructuredResult:
        del messages
        _require_deadline(deadline)
        return response_model.model_validate(self._structured_response)

    async def embed(self, *, texts: Sequence[str], deadline: float) -> list[list[float]]:
        _require_deadline(deadline)
        return [self._stable_embedding(text) for text in texts]

    def _stable_embedding(self, text: str) -> list[float]:
        values: list[float] = []
        counter = 0
        while len(values) < self._embedding_dimensions:
            digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
            for offset in range(0, len(digest), 4):
                integer = int.from_bytes(digest[offset : offset + 4], "big")
                values.append((integer / 2**31) - 1.0)
                if len(values) == self._embedding_dimensions:
                    break
            counter += 1
        return values


class OpenAICompatibleModelAdapter(ChatModelPort, EmbeddingPort):
    """Async adapter for OpenAI-compatible chat and embedding HTTP APIs."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        chat_model: str,
        embedding_model: str,
        timeout_seconds: float = 20.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        if not chat_model.strip() or not embedding_model.strip():
            raise ValueError("model names must not be empty")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._chat_model = chat_model
        self._embedding_model = embedding_model
        self._timeout_seconds = timeout_seconds

    async def chat_structured(
        self,
        *,
        messages: Sequence[ChatMessage],
        response_model: type[StructuredResult],
        deadline: float,
    ) -> StructuredResult:
        payload: dict[str, object] = {
            "model": self._chat_model,
            "messages": [message.to_provider_value() for message in messages],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "strict": True,
                    "schema": response_model.model_json_schema(),
                },
            },
        }
        response = await self._post("chat/completions", payload=payload, deadline=deadline)
        validated: StructuredResult | None = None
        invalid_response = False
        try:
            envelope = response.json()
            content = envelope["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError
            decoded = json.loads(content)
            validated = response_model.model_validate(decoded)
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValidationError):
            invalid_response = True
        if invalid_response:
            raise ModelAdapterError(
                ProviderErrorKind.INVALID_RESPONSE,
                retryable=False,
                status_code=response.status_code,
            )
        if validated is None:
            raise RuntimeError("structured response validation did not produce a model")
        return validated

    async def embed(self, *, texts: Sequence[str], deadline: float) -> list[list[float]]:
        if not texts:
            _require_deadline(deadline)
            return []
        response = await self._post(
            "embeddings",
            payload={"input": list(texts), "model": self._embedding_model},
            deadline=deadline,
        )
        ordered: list[list[float] | None] | None = None
        invalid_response = False
        try:
            envelope = response.json()
            raw_data = envelope["data"]
            if not isinstance(raw_data, list) or len(raw_data) != len(texts):
                raise TypeError
            ordered = [None] * len(texts)
            for item in raw_data:
                if not isinstance(item, dict):
                    raise TypeError
                index = item["index"]
                embedding = item["embedding"]
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or index < 0
                    or index >= len(texts)
                    or ordered[index] is not None
                    or not isinstance(embedding, list)
                    or not embedding
                    or any(not isinstance(value, int | float) for value in embedding)
                ):
                    raise TypeError
                ordered[index] = [float(value) for value in embedding]
            if any(embedding is None for embedding in ordered):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            invalid_response = True
        if invalid_response:
            raise ModelAdapterError(
                ProviderErrorKind.INVALID_RESPONSE,
                retryable=False,
                status_code=response.status_code,
            )
        if ordered is None:
            raise RuntimeError("embedding response validation did not produce vectors")
        return [embedding for embedding in ordered if embedding is not None]

    async def _post(
        self,
        path: str,
        *,
        payload: dict[str, object],
        deadline: float,
    ) -> httpx.Response:
        timeout = min(self._timeout_seconds, _require_deadline(deadline))
        response: httpx.Response | None = None
        transport_error: ProviderErrorKind | None = None
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self._base_url}/{path}",
                    headers={
                        "Authorization": f"Bearer {self._api_key.get_secret_value()}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.TimeoutException:
            transport_error = ProviderErrorKind.TIMEOUT
        except httpx.TransportError:
            transport_error = ProviderErrorKind.UPSTREAM_UNAVAILABLE

        if transport_error is not None:
            raise ModelAdapterError(transport_error, retryable=True)
        if response is None:
            raise RuntimeError("provider request completed without a response")

        _raise_for_provider_status(response.status_code)
        return response


def _require_deadline(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ModelAdapterError(
            ProviderErrorKind.DEADLINE_EXCEEDED,
            retryable=False,
        )
    return remaining


def _raise_for_provider_status(status_code: int) -> None:
    if status_code < 400:
        return
    if status_code in {401, 403}:
        kind = ProviderErrorKind.AUTHENTICATION
        retryable = False
    elif status_code == 429:
        kind = ProviderErrorKind.RATE_LIMITED
        retryable = True
    elif status_code >= 500:
        kind = ProviderErrorKind.UPSTREAM_UNAVAILABLE
        retryable = True
    else:
        kind = ProviderErrorKind.REQUEST_REJECTED
        retryable = False
    raise ModelAdapterError(kind, retryable=retryable, status_code=status_code)
