"""Mock Linear connector. Mirrors jira_mock.py — no ETag support, TTL-only freshness."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from ..plan import SourcePlan, matches_all
from ..settings import CONNECTORS
from .base import Connector, ConnectorContext, ConnectorManifest, FetchResult

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "linear_issues.json"


def _load() -> list[dict]:
    with open(_FIXTURE) as f:
        return json.load(f)


class LinearConnector(Connector):
    name = "linear"

    def __init__(self) -> None:
        self._rows = _load()
        self._cfg = CONNECTORS["linear"]

    def describe(self) -> ConnectorManifest:
        return ConnectorManifest(
            tables=["issues"],
            pushable_filters=list(self._cfg.pushable_filters),
            max_page_size=100,
            supports_conditional_requests=self._cfg.freshness.supports_etag,
            version="1.0.0",
        )

    async def execute(self, plan: SourcePlan, ctx: ConnectorContext) -> FetchResult:
        latency = int(os.environ.get("USQL_LINEAR_LATENCY_MS", self._cfg.sim_latency_ms))
        await asyncio.sleep(latency / 1000)

        if os.environ.get("USQL_LINEAR_FAIL") == "1":
            raise ConnectionError("simulated Linear outage")

        server_filters = plan.pushed_predicates + plan.rls_predicates
        rows = [r for r in self._rows if matches_all(r, server_filters)]
        return FetchResult(rows=rows, etag=None, fetched_at_ms=int(time.time() * 1000))
