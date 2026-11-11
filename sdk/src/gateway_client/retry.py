from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


@dataclass
class RetryPolicy:
    """Retries connect errors, timeouts and 429/502/503/504. Never 400/401/403/422."""

    max_retries: int = 3
    backoff: float = 0.5  # seconds; doubles each attempt
    max_backoff: float = 8.0
    max_retry_after: float = 30.0  # never sleep longer than this because of a Retry-After header
    retry_statuses: frozenset[int] = frozenset({429, 502, 503, 504})
    jitter: bool = True
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)
    async_sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep, repr=False)

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return max(0.0, min(retry_after, self.max_retry_after))
        base = min(self.max_backoff, self.backoff * (2**attempt))
        return base * random.uniform(0.5, 1.0) if self.jitter else base


def parse_retry_after(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:  # HTTP-date form is not supported; fall back to backoff
        return None
