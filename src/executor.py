"""Connector executor: fan-out, post-filter, join, CLS masking, projection.

This file grows across phases. P1 implements single-source fetch + post-filter
+ projection. Join, partial results, masking, and the freshness cache are layered
in by later phases.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .connectors.base import ConnectorContext
from .errors import SOURCE_TIMEOUT
from .freshness import CACHE
from .observability import CONNECTOR_LATENCY, tracer
from .plan import QueryPlan, SourcePlan, matches_all
from .settings import SOURCE_TIMEOUT_MS


@dataclass
class SourceOutcome:
    alias: str
    connector: str
    rows: list[dict]
    freshness_ms: int
    cache_hit: bool = False
    status: str = "ok"          # ok | SOURCE_TIMEOUT | STALE_DATA
    error: str | None = None


@dataclass
class ExecResult:
    columns: list[str]
    rows: list[list]
    freshness_ms: int
    partial: bool = False
    source_status: dict[str, str] = field(default_factory=dict)
    source_detail: list[dict] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)


async def _fetch_source(sp: SourcePlan, ctx: ConnectorContext, registry: dict,
                        max_staleness_ms: int | None = None) -> SourceOutcome:
    connector = registry[sp.connector]
    with tracer.start_as_current_span(f"connector.execute:{sp.connector}") as span:
        start = time.monotonic()
        outcome = await CACHE.fetch(sp, ctx, connector, max_staleness_ms)
        elapsed = time.monotonic() - start
        # post_predicates are applied after cache retrieval (kept out of the key).
        rows = [r for r in outcome.rows if matches_all(r, sp.post_predicates)]
        span.set_attribute("connector", sp.connector)
        span.set_attribute("table", sp.table)
        span.set_attribute("cache_hit", outcome.cache_hit)
        span.set_attribute("rows_fetched", len(rows))
        span.set_attribute("freshness_ms", outcome.freshness_ms)
        CONNECTOR_LATENCY.labels(
            connector=sp.connector, cache_hit=str(outcome.cache_hit).lower()
        ).observe(elapsed)
    return SourceOutcome(
        alias=sp.alias, connector=sp.connector, rows=rows,
        freshness_ms=outcome.freshness_ms, cache_hit=outcome.cache_hit,
        status=outcome.status)


async def _fetch_with_deadline(sp: SourcePlan, ctx: ConnectorContext, registry: dict,
                               max_staleness_ms: int | None) -> SourceOutcome:
    """Isolate each source behind its own deadline so a slow source yields
    partial results instead of failing the whole query (head-of-line blocking)."""
    try:
        return await asyncio.wait_for(
            _fetch_source(sp, ctx, registry, max_staleness_ms),
            timeout=SOURCE_TIMEOUT_MS / 1000)
    except asyncio.TimeoutError:
        return SourceOutcome(alias=sp.alias, connector=sp.connector, rows=[],
                             freshness_ms=0, status=SOURCE_TIMEOUT)


def _project(plan: QueryPlan, joined: list[dict]) -> ExecResult:
    """Build output rows/columns from select_columns over alias-keyed records.

    Applies CLS column masks (defined in the entitlement-injected SourcePlan)
    in the result-merge step, as specified in design doc §7.2.
    """
    from .entitlements import MASKS

    by_alias = {sp.alias: sp for sp in plan.sources}
    columns = [sc.label for sc in plan.select_columns]
    out_rows = []
    for rec in joined:
        row = []
        for sc in plan.select_columns:
            src = rec.get(sc.alias, {})
            value = src.get(sc.column)
            mask_fn = by_alias[sc.alias].masked_columns.get(sc.column)
            if mask_fn:
                value = MASKS[mask_fn](value)
            row.append(value)
        out_rows.append(row)

    if plan.order_by:
        col, desc = plan.order_by
        if col in columns:
            idx = columns.index(col)
            out_rows.sort(key=lambda r: (r[idx] is None, r[idx]), reverse=desc)

    out_rows = out_rows[plan.offset : plan.offset + plan.limit]
    return ExecResult(columns=columns, rows=out_rows, freshness_ms=0)


async def execute_plan(plan: QueryPlan, ctx: ConnectorContext, registry: dict,
                       max_staleness_ms: int | None = None) -> ExecResult:
    outcomes = await asyncio.gather(
        *[_fetch_with_deadline(sp, ctx, registry, max_staleness_ms) for sp in plan.sources]
    )
    by_alias = {o.alias: o for o in outcomes}

    # Single-source: wrap each row under its alias for uniform projection.
    if not plan.join:
        sp = plan.sources[0]
        recs = [{sp.alias: r} for r in by_alias[sp.alias].rows]
        result = _project(plan, recs)
    else:
        recs = _hash_join(plan, by_alias)
        result = _project(plan, recs)

    result.freshness_ms = max((o.freshness_ms for o in outcomes), default=0)
    result.source_status = {o.connector: o.status for o in outcomes}
    result.source_detail = [
        {"connector": o.connector, "version": registry[o.connector].describe().version,
         "cache_hit": o.cache_hit, "freshness_ms": o.freshness_ms, "status": o.status}
        for o in outcomes
    ]
    result.partial = any(o.status == SOURCE_TIMEOUT for o in outcomes)
    for code in ("STALE_DATA", SOURCE_TIMEOUT):
        if any(o.status == code for o in outcomes):
            result.annotations.append(code)
    return result


def _hash_join(plan: QueryPlan, by_alias: dict) -> list[dict]:
    """In-memory hash join: build a hash table from the smaller side, probe
    with the larger side (design doc §10.2). Inner join on the ON-equality."""
    js = plan.join
    left = by_alias[js.left_alias].rows
    right = by_alias[js.right_alias].rows

    # Build from the smaller side to minimise the hash table.
    if len(left) <= len(right):
        build_rows, build_alias, build_col = left, js.left_alias, js.left_column
        probe_rows, probe_alias, probe_col = right, js.right_alias, js.right_column
    else:
        build_rows, build_alias, build_col = right, js.right_alias, js.right_column
        probe_rows, probe_alias, probe_col = left, js.left_alias, js.left_column

    table: dict = {}
    for r in build_rows:
        table.setdefault(r.get(build_col), []).append(r)

    joined: list[dict] = []
    for pr in probe_rows:
        for br in table.get(pr.get(probe_col), []):
            joined.append({build_alias: br, probe_alias: pr})
    return joined
