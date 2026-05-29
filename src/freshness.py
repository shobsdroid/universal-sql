"""Freshness layer: TTL cache + ETag conditional requests + stale-if-error
(design doc §9).

Cache key = {tenant_id, connector, query_fingerprint}. The fingerprint covers
the server-side fetch shape (pushed predicates + RLS), so cached rows are always
the rows the source would return for that scoped fetch. Executor-side
post-filters are applied AFTER cache retrieval, so they never pollute the key.

Prototype uses one in-process dict (the design doc's L1). The L2 Redis tier and
per-tenant KMS encryption are described in the doc but not built here.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .connectors.base import Connector, ConnectorContext
from .plan import SourcePlan
from .settings import CONNECTORS


@dataclass
class CacheEntry:
    rows: list[dict]
    etag: str | None
    fetched_at_ms: int


@dataclass
class FetchOutcome:
    rows: list[dict]            # server-filtered rows (pre executor post-filter)
    freshness_ms: int
    cache_hit: bool
    status: str = "ok"          # ok | STALE_DATA


class FreshnessCache:
    def __init__(self) -> None:
        self._entries: dict[tuple, CacheEntry] = {}

    def _key(self, ctx: ConnectorContext, sp: SourcePlan) -> tuple:
        return (ctx.tenant_id, sp.connector, sp.fingerprint)

    def clear(self) -> None:
        self._entries.clear()

    async def fetch(self, sp: SourcePlan, ctx: ConnectorContext,
                    connector: Connector, max_staleness_ms: int | None) -> FetchOutcome:
        cfg = CONNECTORS[sp.connector].freshness
        # Client staleness tolerance, floored by the source's minimum.
        tolerance = max_staleness_ms if max_staleness_ms is not None else cfg.default_ttl_ms
        tolerance = max(cfg.min_freshness_ms, tolerance)

        key = self._key(ctx, sp)
        entry = self._entries.get(key)
        now = int(time.time() * 1000)

        # Fresh-enough cache hit -> no source call.
        if entry is not None:
            age = now - entry.fetched_at_ms
            if age <= tolerance:
                return FetchOutcome(rows=entry.rows, freshness_ms=age, cache_hit=True)

        # Need a live fetch. Send If-None-Match if we have an ETag.
        if_none_match = entry.etag if (entry and cfg.supports_etag) else None
        ctx_for_fetch = ConnectorContext(
            tenant_id=ctx.tenant_id, user_id=ctx.user_id, if_none_match=if_none_match)

        try:
            result = await connector.execute(sp, ctx_for_fetch)
        except Exception:
            # stale-if-error: serve stale within the configured window.
            if entry is not None:
                age = now - entry.fetched_at_ms
                if age <= cfg.stale_if_error_ms:
                    return FetchOutcome(rows=entry.rows, freshness_ms=age,
                                        cache_hit=True, status="STALE_DATA")
            raise

        if result.not_modified and entry is not None:
            # 304: data unchanged. Reset freshness without re-storing rows.
            entry.fetched_at_ms = result.fetched_at_ms or now
            return FetchOutcome(rows=entry.rows, freshness_ms=0, cache_hit=True)

        self._entries[key] = CacheEntry(
            rows=result.rows, etag=result.etag, fetched_at_ms=result.fetched_at_ms or now)
        return FetchOutcome(rows=result.rows, freshness_ms=0, cache_hit=False)


CACHE = FreshnessCache()
