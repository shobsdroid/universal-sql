"""Rate limiting: three nested token-bucket scopes per connector
(connector -> tenant -> user), each with burst capacity (design doc §8).

In production these are Redis token buckets decremented atomically via a Lua
script. Here they are in-process buckets — same semantics, single node.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .settings import CONNECTORS


@dataclass
class Decision:
    ok: bool
    retry_after_ms: int = 0
    scope: str = ""          # connector | tenant | user
    connector: str = ""
    async_available: bool = False


class _Bucket:
    __slots__ = ("tokens", "capacity", "refill_per_s", "last")

    def __init__(self, rpm: int, burst_multiplier: float):
        self.capacity = rpm * burst_multiplier
        self.tokens = self.capacity
        self.refill_per_s = rpm / 60.0
        self.last = time.monotonic()

    def _refill(self, now: float) -> None:
        elapsed = now - self.last
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_s)
        self.last = now

    def available(self, now: float) -> bool:
        self._refill(now)
        return self.tokens >= 1.0

    def consume(self) -> None:
        self.tokens -= 1.0

    def retry_after_ms(self) -> int:
        deficit = 1.0 - self.tokens
        if deficit <= 0:
            return 0
        return math.ceil(deficit / self.refill_per_s * 1000)


class RateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[tuple, _Bucket] = {}

    def reset(self) -> None:
        self._buckets.clear()

    def _bucket(self, key: tuple, rpm: int, burst: float) -> _Bucket:
        b = self._buckets.get(key)
        if b is None:
            b = _Bucket(rpm, burst)
            self._buckets[key] = b
        return b

    def _scopes(self, connector: str, tenant: str, user: str):
        cfg = CONNECTORS[connector].rate_limit
        burst = cfg.burst_multiplier
        scopes = [
            ("connector", ("c", connector), cfg.connector_rpm),
            ("tenant", ("t", connector, tenant), cfg.tenant_rpm),
            ("user", ("u", connector, tenant, user), cfg.user_rpm),
        ]
        return [(name, self._bucket(key, rpm, burst)) for name, key, rpm in scopes]

    def try_acquire(self, connector: str, tenant: str, user: str) -> Decision:
        return self.try_acquire_many([connector], tenant, user)

    def try_acquire_many(self, connectors: list[str], tenant: str, user: str) -> Decision:
        """Atomic across every bucket of every connector the query touches:
        check (and refill) all buckets first, and only consume if ALL pass.
        A rejected query consumes no budget — matching the design's atomic
        EVALSHA decrement (design doc §8.1)."""
        now = time.monotonic()
        all_buckets = []  # (connector, scope_name, bucket)
        for c in connectors:
            for name, bucket in self._scopes(c, tenant, user):
                all_buckets.append((c, name, bucket))

        for c, name, bucket in all_buckets:
            if not bucket.available(now):
                cfg = CONNECTORS[c].rate_limit
                return Decision(
                    ok=False, retry_after_ms=bucket.retry_after_ms(), scope=name,
                    connector=c, async_available=cfg.async_overflow)

        for _c, _name, bucket in all_buckets:
            bucket.consume()
        return Decision(ok=True)


# Module-level singleton (one process in the prototype).
LIMITER = RateLimiter()
