from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time
from typing import TypeVar


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int
    delays: tuple[float, ...]

    def __post_init__(self) -> None:
        if self.attempts < 1 or len(self.delays) != self.attempts - 1:
            raise ValueError("invalid retry policy")
        if any(delay < 0 for delay in self.delays):
            raise ValueError("invalid retry policy")


DEFAULT_RETRY_POLICY = RetryPolicy(3, (1.0, 2.0))


def retryable_http_status(status: int) -> bool:
    return status in {408, 425, 429} or 500 <= status <= 599


def call_with_retry(
    operation: Callable[[], T],
    should_retry: Callable[[Exception], bool],
    *,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    wait: Callable[[float], None] = time.sleep,
) -> T:
    for attempt in range(policy.attempts):
        try:
            return operation()
        except Exception as error:
            if attempt == policy.attempts - 1 or not should_retry(error):
                raise
            wait(policy.delays[attempt])
    raise AssertionError("unreachable")
