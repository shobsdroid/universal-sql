"""Mock GitHub connector: deterministic fixtures + simulated source behaviour.

Simulates: server-side predicate pushdown, network latency, ETag/304 conditional
requests, and (via env vars) slow or failing sources for partial-result and
stale-if-error demos.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

from ..plan import SourcePlan, matches_all
from ..settings import CONNECTORS
from .base import Connector, ConnectorContext, ConnectorManifest, FetchResult

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "github_pull_requests.json"


def _load() -> list[dict]:
    with open(_FIXTURE) as f:
        return json.load(f)


def _etag(rows: list[dict]) -> str:
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    return f'W/"{digest[:16]}"'


class GitHubConnector(Connector):
    name = "github"

    def __init__(self) -> None:
        self._rows = _load()
        self._cfg = CONNECTORS["github"]

    def describe(self) -> ConnectorManifest:
        return ConnectorManifest(
            tables=["pull_requests"],
            pushable_filters=list(self._cfg.pushable_filters),
            max_page_size=100,
            supports_conditional_requests=self._cfg.freshness.supports_etag,
            version="1.0.0",
        )

    async def execute(self, plan: SourcePlan, ctx: ConnectorContext) -> FetchResult:
        # Simulated source latency (override via env for slow-source demos).
        latency = int(os.environ.get("USQL_GITHUB_LATENCY_MS", self._cfg.sim_latency_ms))
        await asyncio.sleep(latency / 1000)

        if os.environ.get("USQL_GITHUB_FAIL") == "1":
            raise ConnectionError("simulated GitHub outage")

        # Pushdown + RLS applied "at the source": rows the token may not see
        # are never returned. post_predicates are left for the executor.
        server_filters = plan.pushed_predicates + plan.rls_predicates
        rows = [r for r in self._rows if matches_all(r, server_filters)]

        etag = _etag(rows)
        if ctx.if_none_match and ctx.if_none_match == etag:
            return FetchResult(rows=[], etag=etag, not_modified=True,
                               fetched_at_ms=int(time.time() * 1000))

        return FetchResult(rows=rows, etag=etag, fetched_at_ms=int(time.time() * 1000))
