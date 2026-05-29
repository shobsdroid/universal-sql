"""Mock Jira connector. Jira's REST API does not support ETags, so this
connector advertises supports_conditional_requests=False and relies on TTL only.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from ..plan import SourcePlan, matches_all
from ..settings import CONNECTORS
from .base import Connector, ConnectorContext, ConnectorManifest, FetchResult

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "jira_issues.json"


def _load() -> list[dict]:
    with open(_FIXTURE) as f:
        return json.load(f)


class JiraConnector(Connector):
    name = "jira"

    def __init__(self) -> None:
        self._rows = _load()
        self._cfg = CONNECTORS["jira"]

    def describe(self) -> ConnectorManifest:
        return ConnectorManifest(
            tables=["issues"],
            pushable_filters=list(self._cfg.pushable_filters),
            max_page_size=100,
            supports_conditional_requests=self._cfg.freshness.supports_etag,
            version="1.0.0",
        )

    async def execute(self, plan: SourcePlan, ctx: ConnectorContext) -> FetchResult:
        latency = int(os.environ.get("USQL_JIRA_LATENCY_MS", self._cfg.sim_latency_ms))
        await asyncio.sleep(latency / 1000)

        if os.environ.get("USQL_JIRA_FAIL") == "1":
            raise ConnectionError("simulated Jira outage")

        server_filters = plan.pushed_predicates + plan.rls_predicates
        rows = [r for r in self._rows if matches_all(r, server_filters)]
        return FetchResult(rows=rows, etag=None, fetched_at_ms=int(time.time() * 1000))


def get_registry() -> dict:
    """Connector registry — maps connector name to a singleton instance.

    Live connectors (`gh_live`, `jira_live`) are registered only when their
    required env vars are present. Without credentials, the namespace is still
    valid in the schema catalog but the registry omits it — so a query against
    it returns a clear CONNECTOR_AUTH_FAILURE rather than a server error.
    """
    import logging
    import os

    from .github_mock import GitHubConnector
    from .linear_mock import LinearConnector

    log = logging.getLogger(__name__)
    registry: dict = {
        "github": GitHubConnector(),
        "jira": JiraConnector(),
        "linear": LinearConnector(),
    }

    if os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPOS", "").strip():
        try:
            from .github_live import GitHubLiveConnector
            registry["gh_live"] = GitHubLiveConnector()
            log.info("gh_live connector registered")
        except Exception as e:
            log.warning("gh_live not registered: %s", e)

    # JIRA_BASE_URL alone is enough for public Server instances (e.g. Apache).
    # Cloud also needs JIRA_EMAIL + JIRA_API_TOKEN (the connector's __init__ enforces this).
    if os.environ.get("JIRA_BASE_URL"):
        try:
            from .jira_live import JiraLiveConnector
            registry["jira_live"] = JiraLiveConnector()
            log.info("jira_live connector registered")
        except Exception as e:
            log.warning("jira_live not registered: %s", e)

    return registry
