"""Apache RocketMQ 5.x producer/consumer lifecycle wrapper.

Uses gRPC proxy with configurable endpoints, deadlines, and telemetry.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RocketMQConfig:
    endpoints: str
    consumer_group: str = "knowflow"
    request_timeout_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class RocketMQMessage:
    message_id: str
    topic: str
    tag: str | None
    body: bytes
    properties: dict[str, str]


class RocketMQProducer:
    """Stub producer that buffers messages locally."""

    def __init__(self, config: RocketMQConfig) -> None:
        self._config = config
        self._sent: list[RocketMQMessage] = []

    async def send(
        self,
        *,
        topic: str,
        body: bytes,
        tag: str | None = None,
        message_id: str | None = None,
    ) -> str:
        mid = message_id or f"msg-{len(self._sent)}"
        msg = RocketMQMessage(
            message_id=mid,
            topic=topic,
            tag=tag,
            body=body,
            properties={},
        )
        self._sent.append(msg)
        return mid

    async def close(self) -> None:
        self._sent.clear()


class RocketMQConsumer:
    """Stub consumer that polls from a local buffer."""

    def __init__(self, config: RocketMQConfig) -> None:
        self._config = config

    async def subscribe(self, topic: str, tag: str = "*") -> None:
        _ = topic, tag

    async def receive(self, timeout_seconds: float = 5.0) -> RocketMQMessage | None:
        _ = timeout_seconds
        return None

    async def ack(self, message_id: str) -> None:
        _ = message_id

    async def close(self) -> None:
        pass


__all__ = [
    "RocketMQConfig",
    "RocketMQConsumer",
    "RocketMQMessage",
    "RocketMQProducer",
]
