"""In-memory token-bucket rate limiting and login lockout with exponential backoff."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """`capacity` requests per `period` seconds per key, refilled continuously."""

    def __init__(self, capacity: int, period: float = 60.0, max_keys: int = 50_000) -> None:
        self.capacity = max(1, capacity)
        self.rate = self.capacity / period
        self.max_keys = max_keys
        self._buckets: dict[str, _Bucket] = {}

    def check(self, key: str, now: float | None = None) -> float:
        """Consume one token. Returns 0 if allowed, else seconds until retry."""
        now = time.monotonic() if now is None else now
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self.max_keys:
                self._prune(now)
            bucket = self._buckets[key] = _Bucket(float(self.capacity), now)
        bucket.tokens = min(self.capacity, bucket.tokens + (now - bucket.updated) * self.rate)
        bucket.updated = now
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return 0.0
        return (1 - bucket.tokens) / self.rate

    def _prune(self, now: float) -> None:
        full = [
            k
            for k, b in self._buckets.items()
            if b.tokens + (now - b.updated) * self.rate >= self.capacity
        ]
        for k in full:
            del self._buckets[k]
        if len(self._buckets) >= self.max_keys:
            self._buckets.clear()


class Lockout:
    """After `max_failures` consecutive failures, block for base * 2^(n - max) seconds (max 1h).

    Tracked globally (single owner account: an attacker may rotate IPs) and per IP.
    """

    def __init__(
        self, max_failures: int = 5, base_seconds: float = 60.0, cap: float = 3600.0
    ) -> None:
        self.max_failures = max_failures
        self.base = base_seconds
        self.cap = cap
        self._failures: dict[str, int] = {}
        self._until: dict[str, float] = {}

    def blocked_for(self, *keys: str, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        return max((self._until.get(k, 0.0) - now for k in keys), default=0.0) if keys else 0.0

    def fail(self, *keys: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for key in keys:
            n = self._failures.get(key, 0) + 1
            self._failures[key] = n
            if n >= self.max_failures:
                delay = min(self.cap, self.base * 2 ** (n - self.max_failures))
                self._until[key] = now + delay

    def succeed(self, *keys: str) -> None:
        for key in keys:
            self._failures.pop(key, None)
            self._until.pop(key, None)
