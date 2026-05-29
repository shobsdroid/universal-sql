"""Connector SDK interface (design doc §6), reduced to what the prototype needs.

A real connector also implements authenticate()/refreshToken() against the
source's OAuth flow; here the mock connectors assume a valid Vault-leased token
is handed to them via ConnectorContext.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..plan import SourcePlan


@dataclass
class ConnectorManifest:
    tables: list[str]
    pushable_filters: list[str]
    max_page_size: int
    supports_conditional_requests: bool
    # Semver of the connector's contract (schema columns + semantics). Bump major
    # on a breaking schema change, minor on additive, patch on bug fix. Surfaced
    # in /v1/connectors, in each query response's `sources` array, and in audit
    # events so a result is reproducibly attributable to a specific connector
    # version.
    version: str = "1.0.0"


@dataclass
class ConnectorContext:
    tenant_id: str
    user_id: str
    # ETag from a prior response, if the freshness layer has one cached.
    if_none_match: str | None = None


@dataclass
class FetchResult:
    rows: list[dict]
    etag: str | None = None
    not_modified: bool = False          # True if source returned 304
    fetched_at_ms: int = 0              # epoch ms when data was produced


class Connector:
    """Base connector. Subclasses implement describe() and execute()."""

    name: str = "base"

    def describe(self) -> ConnectorManifest:  # pragma: no cover - interface
        raise NotImplementedError

    async def execute(self, plan: SourcePlan, ctx: ConnectorContext) -> FetchResult:  # pragma: no cover
        raise NotImplementedError
