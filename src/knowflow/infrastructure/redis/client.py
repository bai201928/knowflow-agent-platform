"""Redis primitives for checkpoints, key isolation, quotas, and leases.

Redis is deliberately non-authoritative in KnowFlow.  The helpers in this
module keep keys opaque, make mutations atomic, and expose a readiness probe
that distinguishes a live Redis server from one capable of supporting the
LangGraph Redis checkpoint saver.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from pydantic import SecretStr
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local refill_per_second = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local values = redis.call('HMGET', key, 'tokens', 'updated_at_ms')
local tokens = tonumber(values[1]) or capacity
local updated_at_ms = tonumber(values[2]) or now_ms
local elapsed_ms = math.max(0, now_ms - updated_at_ms)
tokens = math.min(capacity, tokens + (elapsed_ms * refill_per_second / 1000))

local allowed = 0
local retry_after_ms = 0
if tokens >= requested then
  allowed = 1
  tokens = tokens - requested
elseif refill_per_second > 0 then
  retry_after_ms = math.ceil((requested - tokens) * 1000 / refill_per_second)
else
  retry_after_ms = -1
end

redis.call('HSET', key, 'tokens', tokens, 'updated_at_ms', now_ms)
if refill_per_second > 0 then
  local ttl_ms = math.max(1000, math.ceil(capacity * 1000 / refill_per_second) * 2)
  redis.call('PEXPIRE', key, ttl_ms)
end
return {allowed, math.floor(tokens), retry_after_ms}
"""  # noqa: S105 - executable script, not a credential


LEASE_RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


LEASE_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class _CapabilityClient(Protocol):
    async def ping(self) -> bool: ...

    async def execute_command(self, *args: object) -> object: ...


class _ScriptClient(Protocol):
    async def eval(self, script: str, numkeys: int, *args: object) -> object: ...


class _LeaseClient(_ScriptClient, Protocol):
    async def set(self, key: str, value: str, **kwargs: object) -> object: ...


def _digest(value: str) -> str:
    """Return a stable opaque identifier without retaining input material."""

    return sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RedisKeyspace:
    """Build typed, deployment-scoped Redis keys from opaque digests."""

    prefix: str

    def __post_init__(self) -> None:
        normalized = self.prefix.strip(": ")
        if not normalized or any(character.isspace() for character in normalized):
            raise ValueError("Redis key prefix must be non-empty and contain no whitespace")
        object.__setattr__(self, "prefix", normalized)

    def _key(self, kind: str, *material: str) -> str:
        framed = "\x1f".join((kind, *material))
        return f"{self.prefix}:{kind}:{_digest(framed)}"

    def session_key(self, subject: str) -> str:
        return self._key("session", subject)

    def cache_key(self, namespace: str, material: str) -> str:
        return self._key("cache", namespace, material)

    def lease_key(self, resource_type: str, resource_id: str) -> str:
        return self._key("lease", resource_type, resource_id)

    def token_bucket_key(self, subject: str, action: str) -> str:
        return self._key("token-bucket", subject, action)

    @property
    def checkpoint_prefix(self) -> str:
        return f"{self.prefix}:checkpoint"

    @property
    def checkpoint_write_prefix(self) -> str:
        return f"{self.prefix}:checkpoint-write"


@dataclass(frozen=True, slots=True)
class RedisCapabilities:
    alive: bool
    redis_json: bool
    redis_search: bool
    database_zero: bool = True

    @property
    def checkpoint_ready(self) -> bool:
        return self.alive and self.redis_json and self.redis_search and self.database_zero

    @property
    def missing(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.redis_json:
            missing.append("RedisJSON")
        if not self.redis_search:
            missing.append("RediSearch")
        if not self.database_zero:
            missing.append("Redis database 0")
        return tuple(missing)


@dataclass(frozen=True, slots=True)
class TokenBucketDecision:
    allowed: bool
    remaining: int
    retry_after_ms: int


def create_async_redis_client(url: SecretStr) -> Redis:
    """Create a binary-response async client from a secret Redis URL.

    Validation errors intentionally omit the supplied URL so credentials can
    never appear in exception messages or logs.
    """

    secret_url = url.get_secret_value()
    try:
        scheme = urlsplit(secret_url).scheme.lower()
    except ValueError as exc:
        raise ValueError("Redis URL is malformed") from exc
    if scheme not in {"redis", "rediss"}:
        raise ValueError("Redis URL must use the redis or rediss scheme")
    return Redis.from_url(
        secret_url,
        decode_responses=False,
        health_check_interval=30,
    )


def _uses_checkpoint_database(client: object) -> bool:
    """Return whether a Redis client selects database zero.

    ``redis-py`` normalizes URL path databases into the connection pool, but
    callers and test doubles may supply that value as either an integer or a
    string.  Missing database configuration has Redis' normal database-zero
    meaning.  An unparseable explicit value is rejected conservatively.
    """

    pool = getattr(client, "connection_pool", None)
    connection_kwargs = getattr(pool, "connection_kwargs", None)
    if not isinstance(connection_kwargs, dict):
        return True
    database = connection_kwargs.get("db", 0)
    if database is None or database == "":
        return True
    try:
        return int(database) == 0
    except (TypeError, ValueError):
        return False


async def probe_redis_capabilities(client: _CapabilityClient) -> RedisCapabilities:
    """Probe liveness plus RedisJSON and RediSearch without mutating data."""

    try:
        alive = bool(await client.ping())
    except (ConnectionError, OSError, RedisError):
        return RedisCapabilities(alive=False, redis_json=False, redis_search=False)
    if not alive:
        return RedisCapabilities(alive=False, redis_json=False, redis_search=False)

    redis_json = True
    redis_search = True
    try:
        await client.execute_command("JSON.GET", "__knowflow:capability-probe__")
    except (ResponseError, RedisError):
        redis_json = False
    try:
        await client.execute_command("FT._LIST")
    except (ResponseError, RedisError):
        redis_search = False
    return RedisCapabilities(
        alive=True,
        redis_json=redis_json,
        redis_search=redis_search,
        database_zero=_uses_checkpoint_database(client),
    )


async def create_checkpoint_saver(
    redis_client: object,
    *,
    keyspace: RedisKeyspace,
) -> AsyncRedisSaver:
    """Create the shared checkpoint saver and ensure its indices exist."""

    if not _uses_checkpoint_database(redis_client):
        raise ValueError("Redis checkpoints require database 0")
    saver = AsyncRedisSaver(
        redis_client=cast(Any, redis_client),
        checkpoint_prefix=keyspace.checkpoint_prefix,
        checkpoint_write_prefix=keyspace.checkpoint_write_prefix,
    )
    await saver.asetup()
    return saver


class RedisTokenBucket:
    """Atomic Redis-backed token bucket suitable for concurrent workers."""

    def __init__(self, client: _ScriptClient) -> None:
        self._client = client

    async def consume(
        self,
        key: str,
        *,
        capacity: int,
        refill_rate_per_second: float,
        requested: int,
        now_ms: int,
    ) -> TokenBucketDecision:
        if capacity <= 0 or requested <= 0 or refill_rate_per_second < 0:
            raise ValueError("Token bucket values are outside the supported range")
        raw = await self._client.eval(
            TOKEN_BUCKET_LUA,
            1,
            key,
            now_ms,
            capacity,
            refill_rate_per_second,
            requested,
        )
        result = cast(list[int], raw)
        if len(result) != 3:
            raise RuntimeError("Redis token bucket returned an invalid response")
        return TokenBucketDecision(
            allowed=bool(result[0]),
            remaining=int(result[1]),
            retry_after_ms=int(result[2]),
        )


class RedisLeaseManager:
    """Owner-bound distributed leases with compare-and-mutate semantics."""

    def __init__(self, client: _LeaseClient) -> None:
        self._client = client

    @staticmethod
    def _succeeded(result: object) -> bool:
        if isinstance(result, (list, tuple)):
            return bool(result) and int(result[0]) == 1
        return int(cast(Any, result)) == 1

    async def acquire(self, key: str, *, owner: str, ttl_ms: int) -> bool:
        if not owner or ttl_ms <= 0:
            raise ValueError("Lease owner and positive ttl_ms are required")
        return bool(await self._client.set(key, owner, nx=True, px=ttl_ms))

    async def renew(self, key: str, *, owner: str, ttl_ms: int) -> bool:
        if not owner or ttl_ms <= 0:
            raise ValueError("Lease owner and positive ttl_ms are required")
        result = await self._client.eval(LEASE_RENEW_LUA, 1, key, owner, ttl_ms)
        return self._succeeded(result)

    async def release(self, key: str, *, owner: str) -> bool:
        if not owner:
            raise ValueError("Lease owner is required")
        result = await self._client.eval(LEASE_RELEASE_LUA, 1, key, owner)
        return self._succeeded(result)
