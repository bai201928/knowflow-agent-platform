"""Executable contract for the T022 Redis foundation.

The unit tests stay offline.  The final test is opt-in and may target only a
disposable Redis database through ``KNOWFLOW_TEST_REDIS_URL``; every key it
creates is namespaced and removed in ``finally``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from knowflow.infrastructure.redis import client as redis_foundation
from knowflow.infrastructure.redis.client import (
    LEASE_RELEASE_LUA,
    LEASE_RENEW_LUA,
    TOKEN_BUCKET_LUA,
    RedisKeyspace,
    RedisLeaseManager,
    RedisTokenBucket,
    create_async_redis_client,
    create_checkpoint_saver,
    probe_redis_capabilities,
)


@dataclass
class ConnectionPoolStub:
    """Expose the URL-selected Redis database without opening a connection."""

    connection_kwargs: dict[str, object]


class CapabilityRedis:
    """Minimal async Redis double for separate liveness/capability probes."""

    def __init__(
        self,
        *,
        alive: bool = True,
        redis_json: bool = True,
        redis_search: bool = True,
        database: int = 0,
    ) -> None:
        self.alive = alive
        self.redis_json = redis_json
        self.redis_search = redis_search
        self.connection_pool = ConnectionPoolStub(connection_kwargs={"db": database})
        self.commands: list[tuple[object, ...]] = []

    async def ping(self) -> bool:
        if not self.alive:
            raise ConnectionError("redis is unavailable")
        return True

    async def execute_command(self, *args: object) -> object:
        self.commands.append(args)
        command = str(args[0]).upper()
        if command == "JSON.GET":
            if not self.redis_json:
                raise ResponseError("unknown command 'JSON.GET'")
            return None
        if command == "FT._LIST":
            if not self.redis_search:
                raise ResponseError("unknown command 'FT._LIST'")
            return []
        raise AssertionError(f"unexpected capability command: {args!r}")


class ScriptRedis:
    """Records script calls and supplies deterministic Redis protocol results."""

    def __init__(self, *results: list[int]) -> None:
        self.results = iter(results)
        self.eval_calls: list[tuple[str, int, tuple[object, ...]]] = []
        self.set_calls: list[tuple[str, str, dict[str, object]]] = []

    async def eval(self, script: str, numkeys: int, *args: object) -> list[int]:
        self.eval_calls.append((script, numkeys, args))
        return next(self.results)

    async def set(self, key: str, value: str, **kwargs: object) -> bool:
        self.set_calls.append((key, value, kwargs))
        return True


def test_async_client_factory_accepts_only_redis_urls_and_uses_binary_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "factory-password-must-not-leak"
    url = f"redis://:{secret}@redis.internal:6379/0"
    sentinel = object()
    captured: dict[str, object] = {}

    def fake_from_url(received_url: str, **kwargs: object) -> object:
        captured["url"] = received_url
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(Redis, "from_url", staticmethod(fake_from_url))

    client = create_async_redis_client(SecretStr(url))

    assert client is sentinel
    assert captured["url"] == url
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["decode_responses"] is False
    assert int(kwargs["health_check_interval"]) > 0

    unsafe = f"https://:{secret}@redis.internal/0"
    with pytest.raises(ValueError) as caught:
        create_async_redis_client(SecretStr(unsafe))
    assert secret not in str(caught.value)
    assert unsafe not in str(caught.value)


def test_keyspace_is_stable_typed_and_never_embeds_raw_sensitive_material() -> None:
    first = RedisKeyspace(prefix="knowflow:test")
    second = RedisKeyspace(prefix="knowflow:test")
    subject = "user@example.internal"
    cache_material = "confidential incident report"
    resource_id = "workflow-customer-123"
    action = "download/payroll"

    keys = {
        "session": first.session_key(subject),
        "cache": first.cache_key("retrieval", cache_material),
        "lease": first.lease_key("workflow", resource_id),
        "token-bucket": first.token_bucket_key(subject, action),
    }

    assert keys == {
        "session": second.session_key(subject),
        "cache": second.cache_key("retrieval", cache_material),
        "lease": second.lease_key("workflow", resource_id),
        "token-bucket": second.token_bucket_key(subject, action),
    }
    for kind, key in keys.items():
        assert key.startswith(f"knowflow:test:{kind}:")
        assert subject not in key
        assert cache_material not in key
        assert resource_id not in key
        assert action not in key
        assert "@" not in key
        assert "/" not in key

    assert first.checkpoint_prefix == "knowflow:test:checkpoint"
    assert first.checkpoint_write_prefix == "knowflow:test:checkpoint-write"


async def test_ping_success_without_json_or_search_is_not_checkpoint_ready() -> None:
    client = CapabilityRedis(redis_json=False, redis_search=False)

    capabilities = await probe_redis_capabilities(client)

    assert capabilities.alive is True
    assert capabilities.redis_json is False
    assert capabilities.redis_search is False
    assert capabilities.checkpoint_ready is False
    assert set(capabilities.missing) == {"RedisJSON", "RediSearch"}
    assert client.commands == [
        ("JSON.GET", "__knowflow:capability-probe__"),
        ("FT._LIST",),
    ]


async def test_redis_json_search_and_db_zero_are_checkpoint_ready() -> None:
    client = CapabilityRedis(database=0)

    capabilities = await probe_redis_capabilities(client)

    assert capabilities.alive is True
    assert capabilities.redis_json is True
    assert capabilities.redis_search is True
    assert capabilities.checkpoint_ready is True
    assert capabilities.missing == ()


async def test_nonzero_database_is_not_checkpoint_ready_even_when_modules_exist() -> None:
    client = CapabilityRedis(database=15)

    capabilities = await probe_redis_capabilities(client)

    assert capabilities.alive is True
    assert capabilities.redis_json is True
    assert capabilities.redis_search is True
    assert capabilities.checkpoint_ready is False
    assert capabilities.missing == ("Redis database 0",)
    assert client.commands == [
        ("JSON.GET", "__knowflow:capability-probe__"),
        ("FT._LIST",),
    ]


async def test_failed_ping_short_circuits_optional_capability_probes() -> None:
    client = CapabilityRedis(alive=False)

    capabilities = await probe_redis_capabilities(client)

    assert capabilities.alive is False
    assert capabilities.checkpoint_ready is False
    assert client.commands == []


async def test_checkpoint_factory_uses_shared_async_client_and_creates_indices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = CapabilityRedis(database=0)
    keyspace = RedisKeyspace(prefix="knowflow:test")

    @dataclass
    class FakeSaver:
        redis_client: object
        checkpoint_prefix: str
        checkpoint_write_prefix: str
        setup_calls: int = 0

        async def asetup(self) -> None:
            self.setup_calls += 1

    def fake_saver(**kwargs: object) -> FakeSaver:
        return FakeSaver(**kwargs)

    monkeypatch.setattr(redis_foundation, "AsyncRedisSaver", fake_saver)

    saver = await create_checkpoint_saver(redis_client, keyspace=keyspace)

    assert saver.redis_client is redis_client
    assert saver.checkpoint_prefix == keyspace.checkpoint_prefix
    assert saver.checkpoint_write_prefix == keyspace.checkpoint_write_prefix
    assert saver.setup_calls == 1


async def test_checkpoint_factory_rejects_nonzero_database_before_search_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis_client = CapabilityRedis(database=15)
    keyspace = RedisKeyspace(prefix="knowflow:test")
    constructor_calls = 0

    def unexpected_saver(**kwargs: object) -> object:
        del kwargs
        nonlocal constructor_calls
        constructor_calls += 1
        raise AssertionError("AsyncRedisSaver must not be constructed for a nonzero database")

    monkeypatch.setattr(redis_foundation, "AsyncRedisSaver", unexpected_saver)

    with pytest.raises(ValueError, match=r"^Redis checkpoints require database 0$"):
        await create_checkpoint_saver(redis_client, keyspace=keyspace)

    assert constructor_calls == 0
    assert redis_client.commands == []


def test_compose_uses_configurable_pinned_redis_image() -> None:
    compose = (Path(__file__).parents[2] / "docker-compose.yml").read_text(encoding="utf-8")

    assert "image: redis:${REDIS_IMAGE_TAG:-8.2.7}" in compose
    assert "image: redis:8.2.7" not in compose


async def test_token_bucket_consumption_is_one_atomic_lua_evaluation() -> None:
    client = ScriptRedis([1, 7, 0])
    limiter = RedisTokenBucket(client)

    decision = await limiter.consume(
        "knowflow:test:token-bucket:abc",
        capacity=10,
        refill_rate_per_second=2.5,
        requested=1,
        now_ms=1_000,
    )

    assert decision.allowed is True
    assert decision.remaining == 7
    assert decision.retry_after_ms == 0
    assert client.eval_calls == [
        (
            TOKEN_BUCKET_LUA,
            1,
            ("knowflow:test:token-bucket:abc", 1_000, 10, 2.5, 1),
        )
    ]


async def test_owner_bound_lease_operations_are_atomic_and_parse_wrong_owner() -> None:
    client = ScriptRedis([1], [0], [0], [1])
    leases = RedisLeaseManager(client)
    key = "knowflow:test:lease:workflow:abc"

    assert await leases.acquire(key, owner="worker-a", ttl_ms=5_000) is True
    assert await leases.renew(key, owner="worker-a", ttl_ms=5_000) is True
    assert await leases.renew(key, owner="worker-b", ttl_ms=5_000) is False
    assert await leases.release(key, owner="worker-b") is False
    assert await leases.release(key, owner="worker-a") is True

    assert client.set_calls == [(key, "worker-a", {"nx": True, "px": 5_000})]
    assert client.eval_calls == [
        (LEASE_RENEW_LUA, 1, (key, "worker-a", 5_000)),
        (LEASE_RENEW_LUA, 1, (key, "worker-b", 5_000)),
        (LEASE_RELEASE_LUA, 1, (key, "worker-b")),
        (LEASE_RELEASE_LUA, 1, (key, "worker-a")),
    ]


def test_lease_lua_scripts_compare_owner_inside_redis_before_mutation() -> None:
    renew = " ".join(LEASE_RENEW_LUA.lower().split())
    release = " ".join(LEASE_RELEASE_LUA.lower().split())

    for script in (renew, release):
        assert "redis.call" in script
        assert "get" in script
        assert "keys[1]" in script
        assert "argv[1]" in script
    assert "pexpire" in renew
    assert "del" in release


@pytest.fixture
async def live_redis_client() -> AsyncIterator[Redis[Any]]:
    url = os.getenv("KNOWFLOW_TEST_REDIS_URL")
    if not url:
        pytest.skip("set KNOWFLOW_TEST_REDIS_URL to a disposable Redis database")
    client = create_async_redis_client(SecretStr(url))
    try:
        yield client
    finally:
        await client.aclose()


@pytest.mark.integration
async def test_live_lease_cannot_be_released_by_wrong_owner_and_is_cleaned_up(
    live_redis_client: Redis[Any],
) -> None:
    """Opt-in semantic check against disposable Redis, never the production DB."""

    key = f"knowflow:test:lease:{uuid4().hex}"
    leases = RedisLeaseManager(live_redis_client)
    try:
        assert await leases.acquire(key, owner="worker-a", ttl_ms=10_000) is True
        assert await leases.release(key, owner="worker-b") is False
        assert await live_redis_client.exists(key) == 1
        assert await leases.release(key, owner="worker-a") is True
        assert await live_redis_client.exists(key) == 0
    finally:
        await live_redis_client.delete(key)
