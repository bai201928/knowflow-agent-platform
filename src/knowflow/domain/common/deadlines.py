from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum

from knowflow.domain.common.errors import ErrorCode, KnowFlowError


class CancellationClass(StrEnum):
    PURE_READ = "PURE_READ"
    BEFORE_DURABLE_ACCEPTANCE = "BEFORE_DURABLE_ACCEPTANCE"
    DURABLY_ACCEPTED = "DURABLY_ACCEPTED"


@dataclass(frozen=True, slots=True)
class Deadline:
    monotonic_at: float

    @classmethod
    def after(cls, seconds: float) -> Deadline:
        if seconds <= 0:
            raise ValueError("deadline duration must be positive")
        return cls(time.monotonic() + seconds)

    @classmethod
    def at(cls, monotonic_at: float) -> Deadline:
        return cls(monotonic_at)

    def remaining_seconds(self) -> float:
        return max(0.0, self.monotonic_at - time.monotonic())

    def expired(self) -> bool:
        return self.remaining_seconds() <= 0

    def timeout_seconds(self, *, maximum: float, minimum: float = 0.001) -> float:
        if maximum <= 0 or minimum <= 0 or minimum > maximum:
            raise ValueError("timeout bounds are invalid")
        remaining = self.remaining_seconds()
        if remaining < minimum:
            raise KnowFlowError(
                ErrorCode.DEADLINE_EXCEEDED,
                "Insufficient absolute deadline budget remains",
                status=504,
                retryable=False,
            )
        return min(maximum, remaining)

    def require_budget(self, minimum_seconds: float = 0.001) -> None:
        if self.remaining_seconds() < minimum_seconds:
            raise KnowFlowError(
                ErrorCode.DEADLINE_EXCEEDED,
                "Insufficient deadline budget remains",
                status=504,
                retryable=False,
            )


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.base_delay_seconds <= 0 or self.max_delay_seconds <= 0:
            raise ValueError("retry delays must be positive")
        if self.base_delay_seconds > self.max_delay_seconds:
            raise ValueError("base delay cannot exceed maximum delay")

    def delay_for_attempt(self, attempt: int) -> float:
        if attempt < 1 or attempt > self.max_attempts:
            raise ValueError("attempt is outside the bounded retry policy")
        exponential_delay = self.base_delay_seconds * float(2 ** (attempt - 1))
        return min(exponential_delay, self.max_delay_seconds)

    def allows_retry(self, *, attempt: int, safe_to_retry: bool, deadline: Deadline) -> bool:
        if not safe_to_retry or attempt >= self.max_attempts or deadline.expired():
            return False
        return deadline.remaining_seconds() >= self.delay_for_attempt(attempt)
